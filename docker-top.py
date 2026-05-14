#!/usr/bin/env python3
"""
docker-top - htop-like TUI for Docker containers, grouped by compose project.
Shows CPU, memory, network, block I/O, PIDs, and status with color coding.
Supports filtering, scrolling, container actions, and keyboard navigation.
"""
import curses
import json
import subprocess
import threading
import time
import re
from collections import defaultdict, OrderedDict

VERSION = "1.7.0"
REFRESH_INTERVAL = 2


def run_cmd(cmd):
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        if result.returncode == 0:
            return [l for l in result.stdout.strip().split('\n') if l]
        return []
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return []


def run_cmd_simple(cmd):
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        return result.returncode, result.stdout.strip(), result.stderr.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
        return -1, '', str(e)


def get_containers():
    lines = run_cmd(['docker', 'ps', '-a', '--format', '{{json .}}'])
    containers = []
    for line in lines:
        try:
            c = json.loads(line)
            labels = {}
            for kv in c.get('Labels', '').split(','):
                if '=' in kv:
                    k, v = kv.split('=', 1)
                    labels[k.strip()] = v.strip()
            c['LabelsDict'] = labels
            c['Project'] = labels.get('com.docker.compose.project', '')
            containers.append(c)
        except (json.JSONDecodeError, ValueError):
            continue
    return containers


def merge_data(containers, stats):
    for c in containers:
        cid = c.get('ID', '')[:12]
        c['Stats'] = stats.get(cid)
    return containers


def group_by_project(containers):
    groups = defaultdict(list)
    standalone = []
    for c in containers:
        p = c.get('Project', '')
        if p:
            groups[p].append(c)
        else:
            standalone.append(c)
    result = OrderedDict()
    for p in sorted(groups.keys()):
        result[p] = sorted(groups[p], key=lambda x: x.get('Names', ''))
    if standalone:
        result[''] = sorted(standalone, key=lambda x: x.get('Names', ''))
    return result


def get_images():
    lines = run_cmd(['docker', 'images', '--format', '{{json .}}'])
    images = []
    for line in lines:
        try:
            img = json.loads(line)
            images.append(img)
        except (json.JSONDecodeError, ValueError):
            continue
    return images


def short_status(state, status):
    s = status.lower()
    if state == 'running':
        m = re.match(r'up\s+(\d+)\s*(minutes?|hours?|days?|weeks?|months?)', s)
        if m:
            return f"Up {m.group(1)}{m.group(2)[0]}"
        return "Running"
    elif state == 'exited':
        m = re.match(r'exited\s*\((\d+)\)\s+(\d+)\s*(.+?)\s*ago', s)
        if m:
            return f"Exit {m.group(1)} {m.group(2)}{m.group(3)[0]}a"
        return "Exited"
    elif state == 'paused':
        return "Paused"
    elif state == 'restarting':
        return "Restarting"
    elif state == 'removing':
        return "Removing"
    elif state == 'dead':
        return "Dead"
    elif state == 'created':
        return "Created"
    return status[:10] if status else "?"


def _pct_bar(val_str, width=5):
    if val_str in ('-', 'N/A'):
        return ' ' * width
    try:
        pct = float(val_str.replace('%', ''))
    except (ValueError, AttributeError):
        return ' ' * width
    filled = max(0, min(width, round(pct / (100 / width))))
    return '█' * filled + '░' * (width - filled)


class DockerTop:
    def __init__(self, stdscr):
        self.stdscr = stdscr
        self.height, self.width = stdscr.getmaxyx()
        self.groups = {}
        self.display_lines = []
        self.scroll_offset = 0
        self.selected_idx = 0
        self.filter_text = ""
        self.is_filtering = False
        self.command_mode = False
        self.running = True
        self.message = ""
        self.message_ts = 0
        self.total_lines = 0
        self.container_count = 0
        self._loading = True
        self.tab = 0  # 0 = Main, 1 = Images

        curses.start_color()
        curses.use_default_colors()
        curses.init_pair(1, curses.COLOR_CYAN, -1)
        curses.init_pair(2, curses.COLOR_GREEN, -1)
        curses.init_pair(3, curses.COLOR_RED, -1)
        curses.init_pair(4, curses.COLOR_YELLOW, -1)
        curses.init_pair(5, curses.COLOR_BLUE, -1)
        curses.init_pair(6, curses.COLOR_WHITE, curses.COLOR_BLUE)
        curses.init_pair(7, curses.COLOR_WHITE, curses.COLOR_GREEN)
        curses.init_pair(8, curses.COLOR_BLACK, curses.COLOR_WHITE)
        curses.init_pair(9, curses.COLOR_MAGENTA, -1)
        curses.init_pair(10, curses.COLOR_WHITE, curses.COLOR_BLACK)

        try:
            curses.curs_set(0)
        except Exception:
            pass
        try:
            curses.set_escdelay(25)
        except Exception:
            pass
        self.stdscr.nodelay(1)

        # streaming docker stats (persistent connection, no repeated overhead)
        self._stream_stats = {}
        self._stats_lock = threading.Lock()
        self._stats_proc = None
        threading.Thread(target=self._stats_stream, daemon=True).start()

        # background container list poller (docker ps -a is fast)
        self._bg_groups = {}
        self._bg_dirty = False
        threading.Thread(target=self._bg_refresh, daemon=True).start()

        # async action tracking
        self._pending = []
        self._pending_lock = threading.Lock()

        # images data
        self._bg_images = []
        self._bg_images_dirty = False
        self._sel_images = set()
        threading.Thread(target=self._bg_images_refresh, daemon=True).start()

        self.hdr_h = 1
        self.ftr_h = 1

    def content_height(self):
        return self.height - self.hdr_h - self.ftr_h

    def _stats_stream(self):
        while self.running:
            try:
                proc = subprocess.Popen(
                    ['docker', 'stats', '--format', '{{json .}}'],
                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                    text=True, bufsize=1
                )
                self._stats_proc = proc
                for line in proc.stdout:
                    if not self.running:
                        break
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        s = json.loads(line)
                        with self._stats_lock:
                            self._stream_stats[s['ID']] = s
                    except (json.JSONDecodeError, KeyError):
                        continue
                proc.wait()
            except Exception:
                pass
            if self.running:
                time.sleep(2)

    def _bg_refresh(self):
        while self.running:
            containers = get_containers()
            with self._stats_lock:
                stats = dict(self._stream_stats)
            merged = merge_data(containers, stats)
            self._bg_groups = group_by_project(merged)
            self._bg_dirty = True
            time.sleep(REFRESH_INTERVAL)

    def _bg_images_refresh(self):
        while self.running:
            self._bg_images = get_images()
            self._bg_images_dirty = True
            time.sleep(REFRESH_INTERVAL * 2)

    def fetch_data(self):
        if self._bg_dirty:
            self._bg_dirty = False
            self.groups = self._bg_groups
            self._loading = False
            return True
        return False

    def _direct_refresh(self):
        containers = get_containers()
        with self._stats_lock:
            stats = dict(self._stream_stats)
        merged = merge_data(containers, stats)
        self.groups = group_by_project(merged)

    def _enqueue_action(self, label, docker_cmd, container_id=None):
        entry = {'label': label, 'cmd': docker_cmd, 'container_id': container_id}
        with self._pending_lock:
            self._pending.append(entry)

        def worker():
            try:
                rc, stdout, stderr = run_cmd_simple(entry['cmd'])
                if rc == 0:
                    self.message = f"{label} done"
                else:
                    self.message = f"{label} failed: {stderr[:50]}"
                self.message_ts = time.time()
            finally:
                with self._pending_lock:
                    if entry in self._pending:
                        self._pending.remove(entry)
                containers = get_containers()
                with self._stats_lock:
                    stats = dict(self._stream_stats)
                merged = merge_data(containers, stats)
                self._bg_groups = group_by_project(merged)
                self._bg_dirty = True

        t = threading.Thread(target=worker, daemon=True)
        t.start()

    def build_display_lines(self):
        if self.tab == 1:
            return self._build_images_lines()
        lines = []
        self.container_count = 0
        ft = self.filter_text.lower().strip() if self.filter_text else ''

        for project, containers in self.groups.items():
            fcontainers = containers
            proj_matches = False
            if ft:
                if project and ft in project.lower():
                    proj_matches = True
                fcontainers = [c for c in containers
                               if ft in c.get('Names', '').lower()]

            if ft and not fcontainers and not proj_matches:
                continue

            if project:
                header = f" Project: {project}"
                lines.append(('pheader', header))
            else:
                header = " Standalone containers"
                lines.append(('sheader', header))
            lines.append(('sep', ''))

            if not fcontainers:
                lines.append(('empty', '  (none match filter)'))
                lines.append(('spacer', ''))
                continue

            lines.append(('colhdr', ''))
            for c in fcontainers:
                lines.append(('row', c))
                self.container_count += 1
            lines.append(('spacer', ''))

        if not lines:
            if self._loading:
                lines.append(('empty', ' Loading containers...'))
            else:
                lines.append(('empty', ' No containers found'))

        return lines

    def _build_images_lines(self):
        lines = []
        ft = self.filter_text.lower().strip() if self.filter_text else ''
        lines.append(('icolhdr', ''))
        for img in self._bg_images:
            repo = img.get('Repository', '<none>') or '<none>'
            tag = img.get('Tag', '<none>') or '<none>'
            iid = img.get('ID', '')
            size = img.get('Size', '?')
            created = img.get('CreatedAt', '?')
            iid_short = iid[:19] if len(iid) > 19 else iid
            sel_key = iid_short
            selected = sel_key in self._sel_images
            if ft and ft not in repo.lower() and ft not in tag.lower():
                continue
            lines.append(('irow', (repo, tag, iid_short, size, created, selected)))
        if len(lines) == 1:
            lines.append(('empty', ' No images found'))
        return lines

    def render_row(self, c):
        cid = c.get('ID', '?')
        name = c.get('Names', '?')
        state = c.get('State', '?')
        status = c.get('Status', '')
        image = c.get('Image', '?')[:30]
        ports = c.get('Ports', '')[:40]

        cid_short = cid[:12] if len(cid) > 12 else cid

        if c.get('Stats'):
            s = c['Stats']
            cpu = s.get('CPUPerc', 'N/A')
            mem_p = s.get('MemPerc', 'N/A')
            mem_u = s.get('MemUsage', 'N/A')
            net = s.get('NetIO', 'N/A')
            blk = s.get('BlockIO', 'N/A')
            pids = s.get('PIDs', 'N/A')
        else:
            cpu = '-'
            mem_p = '-'
            mem_u = '-'
            net = '-'
            blk = '-'
            pids = '-'

        stat = short_status(state, status)
        pending = False
        with self._pending_lock:
            for p in self._pending:
                pid = p.get('container_id')
                if pid and pid[:12] == cid_short:
                    pending = True
                    stat = p['label']
                    break
        cpu_bar = _pct_bar(cpu)
        mem_bar = _pct_bar(mem_p)
        return (cid_short, name, stat, state, cpu_bar, mem_bar, mem_u, net, blk, pids, ports, image, pending)

    def draw_cols(self, w, y, x, width):
        cols = (f" {'ID':<12} {'NAME':<22} {'STATUS':<12} {'CPU':<5} {'MEM':<5} {'MEM USAGE':<22} {'NET I/O':<18} {'BLOCK I/O':<18} {'PIDS':>5}")
        if width < len(cols):
            cols = cols[:width]
        try:
            w.addstr(y, x, cols, curses.color_pair(5) | curses.A_BOLD)
        except Exception:
            pass

    def draw_row(self, w, y, x, width, row_data, selected=False):
        cid, name, stat, state, cpu_bar, mem_bar, mem_u, net, blk, pids, ports, image, pending = row_data
        n = name[:22].ljust(22) if len(name) > 22 else name.ljust(22)
        c = cid[:12].ljust(12)
        s = stat[:12].ljust(12)
        fmt = f" {c} {n} {s} {cpu_bar} {mem_bar} {mem_u:22} {net:18} {blk:18} {pids:>5}"
        if len(fmt) > width:
            fmt = fmt[:width]

        if selected:
            attr = curses.A_REVERSE
        elif pending:
            attr = curses.color_pair(4) | curses.A_BOLD
        elif state in ('running',):
            attr = curses.color_pair(2)
        elif state in ('exited', 'dead'):
            attr = curses.color_pair(3)
        elif state in ('paused',):
            attr = curses.color_pair(4)
        else:
            attr = curses.A_NORMAL

        try:
            w.addstr(y, x, fmt, attr)
        except Exception:
            pass

    def _is_selectable(self, lt):
        return lt in ('row', 'pheader', 'irow')

    def find_prev_row(self):
        idx = self.selected_idx - 1
        while idx >= 0:
            if self._is_selectable(self.display_lines[idx][0]):
                return idx
            idx -= 1
        return self.selected_idx

    def find_next_row(self):
        idx = self.selected_idx + 1
        while idx < len(self.display_lines):
            if self._is_selectable(self.display_lines[idx][0]):
                return idx
            idx += 1
        return self.selected_idx

    def find_first_row(self):
        for idx in range(len(self.display_lines)):
            if self._is_selectable(self.display_lines[idx][0]):
                return idx
        return 0

    def find_last_row(self):
        for idx in range(len(self.display_lines) - 1, -1, -1):
            if self._is_selectable(self.display_lines[idx][0]):
                return idx
        return 0

    def get_selected(self):
        if 0 <= self.selected_idx < len(self.display_lines):
            lt, data = self.display_lines[self.selected_idx]
            if lt == 'row':
                return ('container', data)
            if lt == 'pheader':
                raw = str(data).strip()
                if raw.startswith('Project: '):
                    return ('project', raw[len('Project: '):])
                return ('project', raw)
            if lt == 'irow':
                return ('image', data)
        return None

    def blocking_confirm(self, prompt):
        h, w = self.stdscr.getmaxyx()
        self.stdscr.nodelay(0)
        try:
            curses.curs_set(1)
        except Exception:
            pass
        line = (prompt + " (y/N) ")[:w-1]
        try:
            self.stdscr.addstr(h-1, 0, line.ljust(w-1), curses.A_REVERSE)
        except Exception:
            pass
        self.stdscr.refresh()
        while True:
            k = self.stdscr.getch()
            if k in (ord('y'), ord('Y')):
                self.stdscr.nodelay(1)
                try:
                    curses.curs_set(0)
                except Exception:
                    pass
                return True
            if k in (ord('n'), ord('N'), 27, 10, 13, ord('q'), -1):
                self.stdscr.nodelay(1)
                try:
                    curses.curs_set(0)
                except Exception:
                    pass
                return False

    def _suspend_and_run(self, cmd_list):
        """Suspend curses TUI, run a terminal command, then resume."""
        self.stdscr.nodelay(0)
        curses.echo()
        curses.nocbreak()
        try:
            curses.curs_set(1)
        except Exception:
            pass
        curses.endwin()

        try:
            subprocess.run(cmd_list, check=False)
        finally:
            curses.cbreak()
            curses.noecho()
            self.stdscr.nodelay(1)
            try:
                curses.curs_set(0)
            except Exception:
                pass
            self.height, self.width = self.stdscr.getmaxyx()
            self._direct_refresh()
            self.stdscr.clear()
            self.stdscr.refresh()

    def draw(self):
        h, w = self.height, self.width = self.stdscr.getmaxyx()
        ch = self.content_height()
        ft = self.ftr_h

        # header — draw tabs with distinct styling
        hdr_base = f" docker-top v{VERSION} "
        binds = (f"[q]:q uit  [f]/ filter  [r]efresh  "
                 f"[s]top [S]tart [R]estart  [d]elete  [p]ause [P]unpause  "
                 f"[\u2191\u2193/j/k]sel  [h]elp")
        tab_labels = ["Main", "Images"]
        x = 0
        try:
            self.stdscr.addstr(0, x, hdr_base, curses.color_pair(6))
            x += len(hdr_base)
            for i, label in enumerate(tab_labels):
                tab = f" {label} "
                if i == self.tab:
                    self.stdscr.addstr(0, x, tab, curses.color_pair(6) | curses.A_REVERSE)
                else:
                    self.stdscr.addstr(0, x, tab, curses.A_DIM)
                x += len(tab)
            self.stdscr.addstr(0, x, f" {binds}", curses.color_pair(6))
        except Exception:
            self.stdscr.addstr(0, 0, (hdr_base + " ".join(tab_labels) + " " + binds)[:w])

        # build display lines
        self.display_lines = self.build_display_lines()
        self.total_lines = len(self.display_lines)

        # ensure selected_idx is valid
        if self.total_lines == 0:
            self.selected_idx = 0
        elif self.selected_idx >= self.total_lines:
            self.selected_idx = self.total_lines - 1

        # auto-scroll to keep selection visible
        max_scroll = max(0, self.total_lines - ch)
        if self.selected_idx < self.scroll_offset:
            self.scroll_offset = self.selected_idx
        elif self.selected_idx >= self.scroll_offset + ch:
            self.scroll_offset = self.selected_idx - ch + 1

        if self.scroll_offset > max_scroll:
            self.scroll_offset = max_scroll
        if self.scroll_offset < 0:
            self.scroll_offset = 0

        visible = self.display_lines[self.scroll_offset:self.scroll_offset + ch]

        # clear content area
        for yy in range(self.hdr_h, h - ft):
            try:
                self.stdscr.move(yy, 0)
                self.stdscr.clrtoeol()
            except Exception:
                pass

        for i, (lt, data) in enumerate(visible):
            yy = self.hdr_h + i
            if yy >= h - ft:
                break
            abs_idx = self.scroll_offset + i

            try:
                if lt == 'pheader':
                    attr = curses.color_pair(10)
                    if abs_idx == self.selected_idx:
                        attr = curses.A_REVERSE
                    self.stdscr.addstr(yy, 0, str(data)[:w], attr)
                elif lt == 'sheader':
                    attr = curses.A_DIM
                    if abs_idx == self.selected_idx:
                        attr = curses.A_REVERSE
                    self.stdscr.addstr(yy, 0, str(data)[:w], attr)
                elif lt == 'sep':
                    sep = " \u2500" * ((w - 2) // 2)
                    if len(sep) > w:
                        sep = sep[:w]
                    self.stdscr.addstr(yy, 0, sep[:w], curses.color_pair(5))
                elif lt == 'colhdr':
                    self.draw_cols(self.stdscr, yy, 0, w)
                elif lt == 'row':
                    row = self.render_row(data)
                    self.draw_row(self.stdscr, yy, 0, w, row, selected=(abs_idx == self.selected_idx))
                elif lt == 'icolhdr':
                    cols = " REPOSITORY               TAG                 IMAGE ID             SIZE          CREATED"
                    if len(cols) > w:
                        cols = cols[:w]
                    try:
                        self.stdscr.addstr(yy, 0, cols, curses.color_pair(5) | curses.A_BOLD)
                    except Exception:
                        pass
                elif lt == 'irow':
                    repo, tag, iid, size, created, selected = data
                    sel_flag = ">\u2502" if selected else " \u2502"
                    repo = repo[:22].ljust(22) if len(repo) > 22 else repo.ljust(22)
                    tag = tag[:18].ljust(18) if len(tag) > 18 else tag.ljust(18)
                    iid = iid[:19].ljust(19) if len(iid) > 19 else iid.ljust(19)
                    size_s = str(size)[:14].ljust(14) if len(str(size)) > 14 else str(size).ljust(14)
                    created_s = str(created)[:14].ljust(14) if len(str(created)) > 14 else str(created).ljust(14)
                    fmt = f" {repo} {tag} {iid} {size_s} {created_s}"
                    if len(fmt) > w - 2:
                        fmt = fmt[:w - 2]
                    if abs_idx == self.selected_idx:
                        attr = curses.A_REVERSE
                    elif selected:
                        attr = curses.color_pair(4) | curses.A_BOLD
                    else:
                        attr = curses.A_NORMAL
                    try:
                        self.stdscr.addstr(yy, 0, sel_flag, attr)
                        self.stdscr.addstr(yy, 2, fmt, attr)
                    except Exception:
                        pass
                elif lt == 'spacer':
                    pass
                elif lt == 'empty':
                    self.stdscr.addstr(yy, 0, str(data)[:w], curses.A_DIM)
            except Exception:
                pass

        # footer
        try:
            self.stdscr.move(h - ft, 0)
            self.stdscr.clrtoeol()
        except Exception:
            pass

        sel_name = ""
        sel_state = ""
        sel = self.get_selected()
        if sel:
            kind, data = sel
            if kind == 'container':
                sel_name = data.get('Names', '')
                sel_state = data.get('State', '')
            elif kind == 'project':
                sel_name = f"[Project] {data}"
                sel_state = ''
            elif kind == 'image':
                sel_name = f"[Image] {data[0]}:{data[1]}"
                sel_state = ''

        show_interactive = (sel and kind == 'container' and sel_state == 'running')


        # show pending actions with spinner
        with self._pending_lock:
            has_pending = bool(self._pending)

        if has_pending:
            spinner = '|/-\\'[int(time.time() * 6) % 4]
            with self._pending_lock:
                first = self._pending[0]['label']
                extra = f" (+{len(self._pending)-1})" if len(self._pending) > 1 else ""
            msg = f" {spinner} {first}{extra}..."
            if len(msg) > w:
                msg = msg[:w]
            try:
                self.stdscr.addstr(h - ft, 0, msg, curses.color_pair(4) | curses.A_BOLD)
            except Exception:
                pass
        elif self.is_filtering:
            prompt = f" Filter: {self.filter_text}\u2588"
            if len(prompt) > w:
                prompt = prompt[:w]
            try:
                self.stdscr.addstr(h - ft, 0, prompt, curses.A_REVERSE)
            except Exception:
                pass
        elif self.command_mode:
            try:
                self.stdscr.addstr(h - ft, 0, ":".ljust(w-1), curses.color_pair(9))
            except Exception:
                pass
        elif self.message and time.time() - self.message_ts < 4:
            msg = f" {self.message}"
            if len(msg) > w:
                msg = msg[:w]
            try:
                self.stdscr.addstr(h - ft, 0, msg, curses.color_pair(4))
            except Exception:
                pass
        else:
            sel_tag = f"[{sel_name}]" if sel_name and not sel_name.startswith('[') else sel_name
            if show_interactive:
                st = (f" {sel_tag} running"
                       f"  |  {self.container_count} containers  |  "
                       f"filter: {'\"' + self.filter_text + '\"' if self.filter_text else '(none)'}"
                       f"  |  [e]sh [>]log [<]cfg")
            elif self.tab == 1:
                count = len(self._bg_images)
                sel_count = len(self._sel_images)
                sel_info = f"  |  {sel_count} selected" if sel_count else ""
                st = (f" {sel_tag}  |  {count} images{sel_info}  |  "
                       f"filter: {'\"' + self.filter_text + '\"' if self.filter_text else '(none)'}"
                       f"  |  [Space]sel [u]clear [a]all")
            else:
                st = (f" {sel_tag} {sel_state}  |  {self.container_count} containers  |  "
                       f"lines {self.scroll_offset+1}-{self.scroll_offset+len(visible)}/{self.total_lines}  |  "
                       f"filter: {'\"' + self.filter_text + '\"' if self.filter_text else '(none)'}")
            if len(st) > w:
                st = st[:w]
            try:
                self.stdscr.addstr(h - ft, 0, st, curses.A_DIM)
            except Exception:
                pass

        self.stdscr.refresh()

    def run(self):
        dirty = True

        while self.running:
            if self.fetch_data():
                dirty = True

            if dirty:
                self.draw()
                dirty = False

            key = self.stdscr.getch()
            if key != -1:
                self.handle_key(key)
                dirty = True
            else:
                time.sleep(0.02)

        if self._stats_proc:
            try:
                self._stats_proc.kill()
            except Exception:
                pass
        curses.curs_set(1)
        self.stdscr.erase()
        self.stdscr.refresh()

    def handle_key(self, key):
        if key == -1:
            return

        # --- filter mode ---
        if self.is_filtering:
            if key in (27, 9):
                self.is_filtering = False
                if key == 27:
                    self.filter_text = ""
            elif key in (10, 13, ord('\n'), curses.KEY_ENTER):
                self.is_filtering = False
            elif key in (curses.KEY_BACKSPACE, 127, 8, 263):
                self.filter_text = self.filter_text[:-1]
            elif 32 <= key <= 126:
                self.filter_text += chr(key)
                self.selected_idx = self.find_first_row()
                self.scroll_offset = 0
            return

        # --- command mode (after ':') ---
        if self.command_mode:
            self.command_mode = False
            if key in (ord('q'), ord('Q')):
                self.running = False
            return

        # --- normal mode ---
        # tab switching
        if key in (9, 353):
            self.tab = 1 - self.tab
            self.filter_text = ""
            self.selected_idx = 0
            self.scroll_offset = 0

        # quit
        elif key in (ord('q'), ord('Q')):
            self.running = False

        # esc: clear filter, message, or selection state
        elif key == 27:
            self.filter_text = ""

        # command mode trigger
        elif key == ord(':'):
            self.command_mode = True

        # filtering
        elif key in (ord('f'), ord('F'), ord('/')):
            self.is_filtering = True
            self.filter_text = ""

        # refresh
        elif key in (ord('r'),):
            if self.tab == 0:
                self._direct_refresh()
            else:
                self._bg_images = get_images()
                self._sel_images.clear()
            self.message = "Refreshed!"
            self.message_ts = time.time()

        # navigation - selection
        elif key in (curses.KEY_DOWN, ord('j')):
            new = self.find_next_row()
            if new != self.selected_idx:
                self.selected_idx = new
        elif key in (curses.KEY_UP, ord('k')):
            new = self.find_prev_row()
            if new != self.selected_idx:
                self.selected_idx = new
        elif key in (curses.KEY_NPAGE,):
            ch = self.content_height()
            for _ in range(ch):
                new = self.find_next_row()
                if new == self.selected_idx:
                    break
                self.selected_idx = new
        elif key == curses.KEY_PPAGE:
            ch = self.content_height()
            for _ in range(ch):
                new = self.find_prev_row()
                if new == self.selected_idx:
                    break
                self.selected_idx = new
        elif key == ord('g'):
            self.selected_idx = self.find_first_row()
        elif key == ord('G'):
            self.selected_idx = self.find_last_row()

        # actions (container or project) — run in background, no blocking
        elif key in (ord('s'),):
            sel = self.get_selected()
            if not sel:
                return
            kind, data = sel
            if kind == 'container':
                if data.get('State') == 'running':
                    name = data.get('Names', '?')
                    cid = data.get('ID', '')
                    if self.blocking_confirm(f"Stop {name}?"):
                        self._enqueue_action(f"Stopping {name}", ['docker', 'stop', cid], container_id=cid)
                else:
                    self.message = f"{data.get('Names', '?')} is not running (state: {data.get('State', '?')})"
                    self.message_ts = time.time()
            elif kind == 'project':
                pname = data
                if self.blocking_confirm(f"Stop ALL containers in project '{pname}'?"):
                    self._enqueue_action(f"Stopping project {pname}",
                                         ['docker', 'compose', '-p', pname, 'stop'])

        elif key in (ord('S'),):
            sel = self.get_selected()
            if not sel:
                return
            kind, data = sel
            if kind == 'container':
                if data.get('State') in ('exited', 'dead'):
                    name = data.get('Names', '?')
                    cid = data.get('ID', '')
                    if self.blocking_confirm(f"Start {name}?"):
                        self._enqueue_action(f"Starting {name}", ['docker', 'start', cid], container_id=cid)
                else:
                    self.message = f"{data.get('Names', '?')} is already running"
                    self.message_ts = time.time()
            elif kind == 'project':
                pname = data
                if self.blocking_confirm(f"Start ALL containers in project '{pname}'?"):
                    self._enqueue_action(f"Starting project {pname}",
                                         ['docker', 'compose', '-p', pname, 'start'])

        elif key in (ord('R'),):
            sel = self.get_selected()
            if not sel:
                return
            kind, data = sel
            if kind == 'container':
                name = data.get('Names', '?')
                cid = data.get('ID', '')
                if self.blocking_confirm(f"Restart {name}?"):
                    self._enqueue_action(f"Restarting {name}", ['docker', 'restart', cid], container_id=cid)
            elif kind == 'project':
                pname = data
                if self.blocking_confirm(f"Restart ALL containers in project '{pname}'?"):
                    self._enqueue_action(f"Restarting project {pname}",
                                         ['docker', 'compose', '-p', pname, 'restart'])

        elif key in (ord('p'),):
            sel = self.get_selected()
            if not sel:
                return
            kind, data = sel
            if kind == 'container':
                if data.get('State') == 'running':
                    name = data.get('Names', '?')
                    cid = data.get('ID', '')
                    if self.blocking_confirm(f"Pause {name}?"):
                        self._enqueue_action(f"Pausing {name}", ['docker', 'pause', cid], container_id=cid)
                else:
                    self.message = f"Can't pause {data.get('Names', '?')} (state: {data.get('State', '?')})"
                    self.message_ts = time.time()

        elif key in (ord('P'),):
            sel = self.get_selected()
            if not sel:
                return
            kind, data = sel
            if kind == 'container':
                if data.get('State') == 'paused':
                    name = data.get('Names', '?')
                    cid = data.get('ID', '')
                    if self.blocking_confirm(f"Unpause {name}?"):
                        self._enqueue_action(f"Unpausing {name}", ['docker', 'unpause', cid], container_id=cid)
                else:
                    self.message = f"{data.get('Names', '?')} is not paused"
                    self.message_ts = time.time()

        elif key in (ord('d'), ord('D')):
            sel = self.get_selected()
            if not sel:
                return
            kind, data = sel
            if kind == 'container':
                name = data.get('Names', '?')
                cid = data.get('ID', '')
                if self.blocking_confirm(f"Remove (force) {name}?"):
                    self._enqueue_action(f"Removing {name}", ['docker', 'rm', '-f', cid], container_id=cid)
            elif kind == 'project':
                pname = data
                if self.blocking_confirm(f"Remove ALL containers in project '{pname}'?"):
                    self._enqueue_action(f"Removing project {pname}",
                                         ['docker', 'compose', '-p', pname, 'down'])

        # enter -> container shell
        elif key in (10, 13, ord('\n'), 343, curses.KEY_ENTER):
            sel = self.get_selected()
            if sel:
                kind, data = sel
                if kind == 'container':
                    name = data.get('Names', '?')
                    cid = data.get('ID', '')
                    if data.get('State') != 'running':
                        self.message = f"{name} is not running"
                        self.message_ts = time.time()
                    else:
                        self._suspend_and_run(
                            ['docker', 'exec', '-it', cid, '/bin/sh'])

        # right -> container logs
        elif key == curses.KEY_RIGHT:
            sel = self.get_selected()
            if sel:
                kind, data = sel
                if kind == 'container':
                    name = data.get('Names', '?')
                    cid = data.get('ID', '')
                    if data.get('State') != 'running':
                        self.message = f"{name} is not running"
                        self.message_ts = time.time()
                    else:
                        self._suspend_and_run(
                            ['sh', '-c', f'docker logs -f {cid} 2>&1 | less -R +F'])

        # left -> container inspect
        elif key == curses.KEY_LEFT:
            sel = self.get_selected()
            if sel:
                kind, data = sel
                if kind == 'container':
                    name = data.get('Names', '?')
                    cid = data.get('ID', '')
                    self._suspend_and_run(
                        ['sh', '-c', f'docker inspect {cid} | less -R'])

        # space -> page down (main) or toggle image selection (images)
        elif key == ord(' '):
            if self.tab == 1:
                sel = self.get_selected()
                if sel and sel[0] == 'image':
                    iid = sel[1][2].strip()
                    if iid in self._sel_images:
                        self._sel_images.discard(iid)
                    else:
                        self._sel_images.add(iid)
            else:
                ch = self.content_height()
                for _ in range(ch):
                    new = self.find_next_row()
                    if new == self.selected_idx:
                        break
                    self.selected_idx = new

        # u -> clear all image selections
        elif key in (ord('u'),):
            if self.tab == 1:
                self._sel_images.clear()

        # a -> select all images
        elif key in (ord('a'),):
            if self.tab == 1:
                self._sel_images.clear()
                for img in self._bg_images:
                    iid = img.get('ID', '')
                    iid_short = iid[:19] if len(iid) > 19 else iid
                    self._sel_images.add(iid_short)

        # help
        elif key in (ord('h'), ord('H'), ord('?')):
            self.show_help()

        elif key == curses.KEY_RESIZE:
            pass

    def show_help(self):
        h, w = self.stdscr.getmaxyx()
        help_lines = [
            " docker-top help",
            "",
            " NAVIGATION",
            "  \u2191/k / \u2193/j     Move selection up/down (project headers too)",
            "  PgUp / PgDown   Move selection up/down one page",
            "  g / G           Jump to first/last item",
            "",
            " TABS",
            "  Tab            Switch between Main (containers) and Images views",
            "",
            " IMAGE SELECTION (Images tab)",
            "  Space          Toggle selection on current image",
            "  a              Select all images",
            "  u              Clear all selections",
            "",
            " FILTERING",
            "  f / F / /       Enter filter mode (type to filter by name or project)",
            "  Enter / Esc     Apply / cancel filter",
            "  Backspace       Delete last character",
            "",
            " CONTAINER / PROJECT ACTIONS",
            "  (select a container row for single-container actions)",
            "  (select a project header for whole-project actions)",
            "  s               Stop container / Stop all in project",
            "  S               Start container / Start all in project",
            "  R               Restart container / Restart all in project",
            "  p               Pause container",
            "  P               Unpause container",
            "  d               Remove container / docker compose down project",
            "",
            " INTERACTIVE (container row only)",
            "  Enter           docker exec -it <container> /bin/sh",
            "  \u2192 (Right)     docker logs -f <container> | less -R +F",
            "  \u2190 (Left)     docker inspect <container> | less -R",
            "                 (press q in less to return)",
            "",
            " MISC",
            "  r               Force refresh data",
            "  q / :q          Quit",
            "  :               Command mode (press q after :)",
            "  h / H / ?       Show this help",
            "",
            " Press any key to close help",
        ]
        box_h = len(help_lines) + 2
        box_w = max(len(l) for l in help_lines) + 4
        box_x = max(0, (w - box_w) // 2)
        box_y = max(0, (h - box_h) // 2)

        try:
            curses.curs_set(1)
        except Exception:
            pass
        self.stdscr.nodelay(0)
        self.stdscr.erase()

        for i in range(box_h):
            for j in range(box_w):
                try:
                    if i == 0 or i == box_h - 1:
                        ch = '\u2500' if 0 < j < box_w - 1 else ('\u250c' if j == 0 else '\u2510')
                    elif j == 0 or j == box_w - 1:
                        ch = '\u2502'
                    else:
                        ch = ' '
                    self.stdscr.addch(box_y + i, box_x + j, ch)
                except Exception:
                    pass

        for i, line in enumerate(help_lines):
            try:
                if line.startswith(" docker-top"):
                    self.stdscr.addstr(box_y + 1 + i, box_x + 2, line, curses.A_BOLD)
                elif line and not line.startswith(" "):
                    self.stdscr.addstr(box_y + 1 + i, box_x + 2, line, curses.color_pair(1) | curses.A_BOLD)
                else:
                    self.stdscr.addstr(box_y + 1 + i, box_x + 2, line)
            except Exception:
                pass

        self.stdscr.refresh()
        self.stdscr.getch()
        self.stdscr.nodelay(1)
        try:
            curses.curs_set(0)
        except Exception:
            pass


def main(stdscr):
    app = DockerTop(stdscr)
    app.run()


if __name__ == '__main__':
    try:
        curses.wrapper(main)
    except KeyboardInterrupt:
        pass
    except Exception as e:
        print(f"docker-top error: {e}", file=__import__('sys').stderr)
        __import__('sys').exit(1)
