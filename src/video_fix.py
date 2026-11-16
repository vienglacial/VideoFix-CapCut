"""VideoFix: local, no-install CapCut video and picture recovery for Windows."""
from pathlib import Path
import argparse
import datetime
import json
import os
import queue
import sys
import threading
import traceback
import uuid

from batch_jobs import collect_jobs, OUTPUT_NAME
from bdve_engine import repair_file, CancelledError
from playback_check import find_ffmpeg, verify_playback
from duplicates import scan_duplicates
from h264_export import make_h264_copy

VERSION = '1.2.0'


def save_reports(records, roots, issues):
    result = []
    stamp = datetime.datetime.now().strftime('%Y%m%d_%H%M%S') + '_' + uuid.uuid4().hex[:6]
    for root in roots:
        try:
            root.mkdir(parents=True, exist_ok=True)
            path = root / ('ket_qua_' + stamp + '.json')
            with path.open('x', encoding='utf-8') as handle:
                json.dump({'version': VERSION, 'records': records, 'issues': issues},
                          handle, ensure_ascii=False, indent=2, default=str)
            result.append(path)
        except OSError as error:
            issues.append(f'Không lưu được nhật ký: {root}: {error}')
    return result


def remove_source(source, destination, info, remove):
    """Move a repaired file's original to the Recycle Bin once that is safe.

    The repaired copy must have passed the full decode check and still have
    its exact size, and the original must be unchanged since it was read.
    Returns ``{"status": "deleted" | "kept" | "error", "reason"}``.
    """
    if not info.get('verification'):
        return {'status': 'kept', 'reason': 'Chưa kiểm tra phát được bản sửa (không tìm thấy FFmpeg/CapCut) '
                                            'nên giữ lại file gốc.'}
    try:
        if info.get('size') is None or destination.stat().st_size != info['size']:
            return {'status': 'kept', 'reason': 'Bản sửa không còn đúng dung lượng nên giữ lại file gốc.'}
        current = source.stat()
        if (current.st_size, current.st_mtime_ns) != (info.get('source_bytes'), info.get('source_mtime_ns')):
            return {'status': 'kept', 'reason': 'File gốc đã thay đổi sau khi phục hồi nên được giữ lại.'}
        remove(source)
    except OSError as error:
        return {'status': 'error', 'reason': str(error)}
    return {'status': 'deleted', 'reason': ''}


def run_jobs(jobs, cancel, emit, check=True, h264=False, delete_source=False, remove=None):
    """Repair ``jobs`` in order; return ``(records, output folders holding a result)``.

    ``check`` fully decodes every result and every already-standard file.
    ``delete_source`` recycles an original only after its copy passed that
    decode, so it turns the decode on for repaired files even without
    ``check``. ``remove(path)`` defaults to the Windows Recycle Bin.
    """
    records, roots = [], []
    ffmpeg = find_ffmpeg() if check or h264 or delete_source else None
    decoder = ffmpeg if check or delete_source else None
    emit('verification', bool(decoder))
    if delete_source and remove is None:
        from duplicate_delete import recycle as remove
    for index, job in enumerate(jobs):
        if cancel.is_set():
            break
        emit('working', index)
        record = {'source': str(job.source), 'destination': str(job.destination)}
        stopped = False
        try:
            if job.destination.exists():
                record.update(status='skipped_existing', reason='Đã có bản sửa; giữ nguyên file đã có.')
            else:
                def progress(value):
                    emit('progress', (index, min(1.0, max(0.0, value))))
                def verify(temp):
                    emit('checking', index)
                    return verify_playback(temp, decoder, cancel)
                info = repair_file(job.source, job.destination,
                                   log=lambda line: emit('log', (index, line)),
                                   progress=progress, cancel=cancel,
                                   verify=verify if decoder else None)
                record.update(info)
                destination = Path(info.get('destination', job.destination))
                if info['status'] == 'skipped_healthy' and check and ffmpeg:
                    emit('checking', index)
                    record['verification'] = verify_playback(job.source, ffmpeg, cancel)
                    record['full_decode_checked'] = True
                if info['status'] == 'repaired':
                    record['full_decode_checked'] = bool(decoder)
                    if h264 and info.get('media') != 'image':
                        codec = info.get('video_info', {}).get('video_codec')
                        if codec == 'h264':
                            record['h264'] = {'status': 'skipped_already_h264'}
                        elif not ffmpeg:
                            record['h264'] = {'status': 'error', 'reason': 'Không tìm thấy FFmpeg/CapCut trên máy.'}
                        else:
                            emit('converting', index)
                            try:
                                record['h264'] = make_h264_copy(destination, ffmpeg, cancel)
                            except CancelledError:
                                record['h264'] = {'status': 'cancelled'}
                                stopped = True
                            except Exception as error:
                                record['h264'] = {'status': 'error', 'reason': str(error)}
                    if delete_source and not stopped:
                        record['source_removed'] = remove_source(job.source, destination, info, remove)
            if record['status'] in {'repaired', 'skipped_existing'} and job.output_root not in roots:
                roots.append(job.output_root)
        except CancelledError:
            record.update(status='cancelled', reason='Đã dừng theo yêu cầu.')
            stopped = True
        except Exception as error:
            record.update(status='error', reason=str(error), detail=traceback.format_exc())
        records.append(record)
        emit('result', (index, record))
        if stopped:
            break
        emit('progress', (index, 1.0))
    return records, roots


def headless(args):
    stop = threading.Event()
    jobs, issues = collect_jobs(args.paths, not args.no_recursive, args.output_dir, stop)
    if getattr(args, 'duplicates', False):
        mp4_paths = [job.source for job in jobs if job.source.suffix.casefold() == '.mp4']
        if not mp4_paths:
            if sys.stdout:
                print(json.dumps({'files': 0, 'errors': issues or ['Không tìm thấy MP4.']}, ensure_ascii=True))
            return 1
        result = scan_duplicates(mp4_paths, cancel=stop)
        if args.output_dir:
            report_root = Path(args.output_dir).resolve()
        elif jobs:
            report_root = jobs[0].output_root
        else:
            if sys.stdout:
                print(json.dumps({'files': 0, 'errors': issues or ['Không tìm thấy MP4.']}))
            return 1
        report_root.mkdir(parents=True, exist_ok=True)
        report_path = report_root / ('mp4_trung_' + uuid.uuid4().hex[:10] + '.json')
        result['input_issues'] = issues
        with report_path.open('x', encoding='utf-8') as handle:
            json.dump(result, handle, ensure_ascii=False, indent=2, default=str)
        if sys.stdout:
            print(json.dumps({'report': str(report_path), 'groups': len(result['groups']),
                              'duplicate_files': result['duplicate_files'], 'errors': len(result['errors'])}))
        return 1 if result['errors'] or issues else 0
    records, roots = run_jobs(jobs, stop, lambda *event: None, not args.no_verify, getattr(args, 'h264', False),
                            getattr(args, 'delete_source', False))
    reports = []
    if getattr(args, 'report', False):     # no log file unless asked for: the summary below goes to stdout
        if args.output_dir:
            roots = [Path(args.output_dir).resolve()]
        elif not roots and jobs:
            roots.append(jobs[0].output_root)
        reports = save_reports(records, roots, issues)
    summary = {'files': len(jobs), 'repaired': sum(r['status'] == 'repaired' for r in records),
               'errors': sum(r['status'] in {'error', 'unsupported'} for r in records),
               'skipped': sum(r['status'].startswith('skipped') for r in records),
               'h264_created': sum(r.get('h264', {}).get('status') == 'created' for r in records),
               'sources_deleted': sum(r.get('source_removed', {}).get('status') == 'deleted' for r in records),
               'reports': [str(p) for p in reports], 'issues': issues}
    if sys.stdout:
        print(json.dumps(summary, ensure_ascii=True))
    return 1 if summary['errors'] or issues or not jobs else 0


def gui(args):
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox
    from win_drop import attach_drop_target
    from duplicate_ui import show_duplicate_results

    class App:
        def __init__(self):
            self.root = tk.Tk()
            self.root.title('VideoFix 1.2 · Phục hồi video & hình CapCut · Quét trùng MP4')
            scale = max(1.0, self.root.winfo_fpixels('1i') / 96)     # keep the layout on high-DPI screens
            width = min(int(980 * scale), self.root.winfo_screenwidth() - 60)
            height = min(int(760 * scale), self.root.winfo_screenheight() - 110)
            self.root.geometry(f'{width}x{height}')
            self.root.minsize(min(width, int(880 * scale)), min(height, int(640 * scale)))
            self.root.configure(bg='#f3f6fb')
            self.root.option_add('*Font', ('Segoe UI', 10))
            self.events = queue.Queue()
            self.stop = threading.Event()
            self.paths, self.jobs, self.output_roots = [], [], []
            self.records = {}
            self.busy, self.close_pending = False, False
            self.pending, self.pending_auto = [], False     # dropped while busy
            self.auto_next = False                          # start once the running scan ends
            self.quiet_scan = False                         # that scan keeps the previous message
            self.batch_done = False                         # the list was fully processed
            self.recursive = tk.BooleanVar(value=not args.no_recursive)
            self.check = tk.BooleanVar(value=not args.no_verify)
            self.h264 = tk.BooleanVar(value=getattr(args, 'h264', False))
            self.auto = tk.BooleanVar(value=not getattr(args, 'no_auto', False))
            self.delete = tk.BooleanVar(value=getattr(args, 'delete_source', False))
            self.log = tk.BooleanVar(value=getattr(args, 'report', False))
            self.message = tk.StringVar(value='Chọn hoặc kéo thả video / hình để phục hồi, hoặc quét trùng MP4.')
            self.count = tk.StringVar(value='Chưa chọn file')
            self.hint = tk.StringVar()
            self.status = tk.StringVar(value=f'Chạy trên máy của bạn · Giữ nguyên chất lượng gốc · v{VERSION}')
            style = ttk.Style()
            style.theme_use('clam')
            style.configure('TFrame', background='#f3f6fb')
            style.configure('TCheckbutton', background='#f3f6fb', foreground='#334155')
            style.configure('TButton', padding=(13, 8), font=('Segoe UI', 10))
            style.configure('Accent.TButton', background='#087f8c', foreground='white', borderwidth=0)
            style.map('Accent.TButton', background=[('active', '#066b76'), ('disabled', '#afbec8')])
            style.configure('Treeview', background='white', fieldbackground='white',
                            foreground='#1f2937', rowheight=31, borderwidth=0)
            style.configure('Treeview.Heading', font=('Segoe UI', 10, 'bold'), padding=(7, 9))
            style.configure('TProgressbar', background='#087f8c', troughcolor='#dce5ef')
            body = ttk.Frame(self.root, padding=25)
            body.pack(fill='both', expand=True)
            tk.Label(body, text='VideoFix', font=('Segoe UI', 25, 'bold'),
                     bg='#f3f6fb', fg='#0f172a').pack(anchor='w')
            tk.Label(body, text='Phục hồi video và hình CapCut · Quét MP4 trùng nội dung dù khác tên',
                     font=('Segoe UI', 11), bg='#f3f6fb', fg='#526277').pack(anchor='w', pady=(0, 18))
            zone = tk.Frame(body, bg='#e6f3f4', highlightbackground='#8bbec4', highlightthickness=1)
            zone.pack(fill='x')
            tk.Label(zone, text='Kéo video, hình hoặc thư mục vào cửa sổ này',
                     font=('Segoe UI', 14, 'bold'), bg='#e6f3f4', fg='#135d66').pack(pady=(18, 5))
            tk.Label(zone, textvariable=self.hint, bg='#e6f3f4', fg='#486775').pack(pady=(0, 12))
            choose = tk.Frame(zone, bg='#e6f3f4')
            choose.pack(pady=(0, 18))
            self.files_button = ttk.Button(choose, text='Chọn file…', command=self.choose_files)
            self.files_button.pack(side='left', padx=5)
            self.folder_button = ttk.Button(choose, text='Chọn thư mục…', command=self.choose_folder)
            self.folder_button.pack(side='left', padx=5)
            options = ttk.Frame(body)
            options.pack(fill='x', pady=(13, 3))
            self.recurse_box = ttk.Checkbutton(options, text='Quét cả thư mục con', variable=self.recursive,
                command=lambda: self.add_paths([]) if self.paths else None)
            self.recurse_box.pack(side='left')
            self.check_box = ttk.Checkbutton(options, text='Kiểm tra phát hình và tiếng', variable=self.check)
            self.check_box.pack(side='left', padx=18)
            self.h264_box = ttk.Checkbutton(options, text='Tạo thêm bản H264 (dễ phát hơn, nén lại)', variable=self.h264)
            self.h264_box.pack(side='left')
            self.clear_button = ttk.Button(options, text='Làm trống danh sách', command=self.clear)
            self.clear_button.pack(side='right')
            extras = ttk.Frame(body)
            extras.pack(fill='x', pady=(0, 11))
            self.auto_box = ttk.Checkbutton(extras, text='Tự chạy ngay khi thả / chọn file', variable=self.auto,
                                            command=self.refresh_hint)
            self.auto_box.pack(side='left')
            self.delete_box = ttk.Checkbutton(extras, text='Xóa file gốc khi phục hồi xong (vào Thùng rác)',
                                              variable=self.delete, command=self.refresh_hint)
            self.delete_box.pack(side='left', padx=18)
            self.log_box = ttk.Checkbutton(extras, text='Lưu nhật ký JSON', variable=self.log)
            self.log_box.pack(side='left')
            table_frame = ttk.Frame(body)
            self.table = ttk.Treeview(table_frame, columns=('file', 'status'), show='headings', selectmode='browse')
            self.table.heading('file', text='File')
            self.table.heading('status', text='Trạng thái')
            self.table.column('file', width=540, minwidth=260)
            self.table.column('status', width=320, minwidth=220)
            scroll = ttk.Scrollbar(table_frame, orient='vertical', command=self.table.yview)
            self.table.configure(yscrollcommand=scroll.set)
            self.table.pack(side='left', fill='both', expand=True)
            self.table.bind('<Double-1>', lambda event: self.show_details())
            scroll.pack(side='right', fill='y')
            for tag, color in [('ok', '#087443'), ('error', '#b42318'), ('skip', '#617186')]:
                self.table.tag_configure(tag, foreground=color)
            footer = ttk.Frame(body)
            footer.pack(side='bottom', fill='x')
            table_frame.pack(side='top', fill='both', expand=True)
            tk.Label(footer, textvariable=self.count, anchor='w', bg='#f3f6fb', fg='#526277').pack(fill='x', pady=(9, 0))
            self.progress = ttk.Progressbar(footer, mode='determinate', maximum=100)
            self.progress.pack(fill='x', pady=(10, 8))
            tk.Label(footer, textvariable=self.message, anchor='w', wraplength=width - 90,
                     justify='left', bg='#f3f6fb', fg='#1f2937').pack(fill='x')
            tk.Label(footer, text=f'Kết quả: thư mục {OUTPUT_NAME} cạnh file gốc hoặc trong thư mục đã chọn.',
                     bg='#f3f6fb', fg='#617186', anchor='w').pack(fill='x', pady=(5, 12))
            actions = ttk.Frame(footer)
            actions.pack(fill='x')
            self.start_button = ttk.Button(actions, text='Bắt đầu phục hồi', style='Accent.TButton',
                                           command=self.start, state='disabled')
            self.start_button.pack(side='left')
            self.stop_button = ttk.Button(actions, text='Dừng', command=self.cancel, state='disabled')
            self.stop_button.pack(side='left', padx=8)
            self.duplicates_button = ttk.Button(actions, text='Quét trùng MP4', command=self.duplicates, state='disabled')
            self.duplicates_button.pack(side='left', padx=(0, 8))
            self.details_button = ttk.Button(actions, text='Chi tiết', command=self.show_details, state='disabled')
            self.details_button.pack(side='left')
            self.open_button = ttk.Button(actions, text='Mở thư mục kết quả', command=self.open_results, state='disabled')
            self.open_button.pack(side='right')
            tk.Label(footer, textvariable=self.status, anchor='w', bg='#f3f6fb', fg='#758397',
                     font=('Segoe UI', 9)).pack(fill='x', pady=(12, 0))
            self.refresh_hint()
            self.root.protocol('WM_DELETE_WINDOW', self.close)
            self.drop_target = attach_drop_target(self.root, self.dropped)
            self.root.after(80, self.pump)
            if args.paths:
                self.root.after(150, lambda: self.add_paths(args.paths, auto=True))

        def refresh_hint(self):
            self.hint.set(('Thả vào là tự nhận và phục hồi luôn.' if self.auto.get()
                           else 'Thả vào, rồi bấm Bắt đầu phục hồi.')
                          + (' File gốc sẽ vào Thùng rác sau khi bản sửa qua kiểm tra phát.' if self.delete.get()
                             else ' File gốc được giữ nguyên.'))

        def refresh_count(self):
            text = f'{len(self.jobs)} file' if self.jobs else 'Chưa chọn file'
            if self.pending:
                text += f' · đã nhận thêm {len(self.pending)} mục, sẽ thêm vào ngay khi xong việc đang chạy'
            self.count.set(text)

        def choose_files(self):
            selected = filedialog.askopenfilenames(title='Chọn video hoặc hình cần phục hồi',
                filetypes=[('Video và hình', '*.mp4 *.mov *.m4v *.mkv *.tmp *.png *.jpg *.jpeg'),
                           ('Tất cả file', '*.*')])
            if selected:
                self.add_paths(selected, auto=True)

        def choose_folder(self):
            selected = filedialog.askdirectory(title='Chọn thư mục video / hình')
            if selected:
                self.add_paths([selected], auto=True)

        def dropped(self, paths):
            self.add_paths(paths, auto=True)

        def set_busy(self, value):
            self.busy = value
            state = 'disabled' if value else 'normal'
            for widget in (self.files_button, self.folder_button, self.clear_button, self.recurse_box,
                           self.check_box, self.h264_box, self.delete_box, self.log_box):
                widget.configure(state=state)
            self.start_button.configure(state='normal' if self.jobs and not value else 'disabled')
            self.duplicates_button.configure(state='normal' if self.jobs and not value else 'disabled')
            self.details_button.configure(state='normal' if self.jobs and not value else 'disabled')
            self.stop_button.configure(state='normal' if value else 'disabled')

        def add_paths(self, paths, auto=False, quiet=False):
            """Add files/folders to the list; ``auto`` asks to start right after the scan.

            ``quiet`` keeps the message of the step that just ended on screen.
            """
            paths = [str(p) for p in paths]
            if self.busy:
                if paths:       # never lose a drop: keep it until the running step ends
                    self.pending.extend(paths)
                    self.pending_auto = self.pending_auto or auto
                    self.refresh_count()
                return
            if paths and self.batch_done:
                self.paths = []     # a finished batch is replaced, not processed twice
            if paths:
                self.batch_done = False
            self.paths.extend(paths)
            self.auto_next = auto and bool(paths)
            self.quiet_scan = quiet
            self.stop.clear()
            self.set_busy(True)
            if not quiet:
                self.message.set('Đang tìm video và hình…')
            recursive, wanted = self.recursive.get(), list(self.paths)
            def scan():
                try:
                    jobs, issues = collect_jobs(wanted, recursive, cancel=self.stop)
                    self.events.put(('scan', (jobs, issues)))
                except Exception as error:
                    self.events.put(('scan_error', str(error)))
            threading.Thread(target=scan, daemon=True).start()

        def flush_pending(self, allow_auto=True):
            """Hand over what was dropped while busy; returns True when a scan started."""
            paths, auto = self.pending, self.pending_auto and allow_auto
            self.pending, self.pending_auto = [], False
            if not paths or self.close_pending:
                return False
            self.add_paths(paths, auto, quiet=not auto)
            return True

        def clear(self):
            self.paths, self.jobs = [], []
            self.records = {}
            self.batch_done = False
            self.table.delete(*self.table.get_children())
            self.start_button.configure(state='disabled')
            self.duplicates_button.configure(state='disabled')
            self.details_button.configure(state='disabled')
            self.progress['value'] = 0
            self.refresh_count()
            self.message.set('Chọn hoặc kéo thả video / hình để phục hồi, hoặc quét trùng MP4.')

        def start(self):
            if self.busy or not self.jobs:
                return
            self.stop.clear()
            self.set_busy(True)
            self.output_roots = []
            self.records = {}
            self.open_button.configure(state='disabled')
            self.progress['value'] = 0
            for index in range(len(self.jobs)):
                self.table.item(str(index), values=(self.jobs[index].source.name, 'Chờ xử lý'), tags=())
            jobs = list(self.jobs)
            check, h264, delete, log = self.check.get(), self.h264.get(), self.delete.get(), self.log.get()
            window = self.root.winfo_id()
            def remove(path):
                from duplicate_delete import recycle
                recycle(path, window)
            def worker():
                try:
                    records, roots = run_jobs(jobs, self.stop,
                        lambda kind, payload: self.events.put((kind, payload)), check, h264, delete, remove)
                    issues, reports = [], []
                    if log:
                        reports = save_reports(records, roots or [jobs[0].output_root], issues)
                    self.events.put(('done', (records, roots, reports, issues)))
                except Exception as error:
                    self.events.put(('fatal', str(error)))
            threading.Thread(target=worker, daemon=True).start()

        def duplicates(self):
            if self.busy or not self.jobs:
                return
            paths = [job.source for job in self.jobs if job.source.suffix.casefold() == '.mp4']
            if not paths:
                self.message.set('Danh sách hiện tại chưa có file MP4.')
                return
            self.stop.clear()
            self.set_busy(True)
            self.progress['value'] = 0
            self.message.set('Đang quét MP4 trùng nội dung…')
            def worker():
                try:
                    result = scan_duplicates(paths,
                        log=lambda text: self.events.put(('duplicate_log', text)),
                        progress=lambda fraction: self.events.put(('duplicate_progress', fraction)),
                        cancel=self.stop)
                    self.events.put(('duplicates_done', result))
                except Exception as error:
                    self.events.put(('fatal', str(error)))
            threading.Thread(target=worker, daemon=True).start()

        def show_details(self):
            selection = self.table.selection()
            if not selection:
                self.message.set('Chọn một dòng trong danh sách rồi bấm Chi tiết.')
                return
            index = int(selection[0])
            if index >= len(self.jobs):
                return
            job = self.jobs[index]
            record = self.records.get(index, {})
            info = record.get('video_info', {})
            lines = [f'File: {job.source.name}', f'Đường dẫn: {job.source}', '',
                     f'Trạng thái: {self.table.item(selection[0], "values")[1]}']
            if record.get('reason'):
                lines.extend(['', 'Lý do: ' + str(record['reason'])])
            if record.get('media') == 'image' and info:
                lines.extend(['', f'Hình: {str(info.get("format", "")).upper()} · '
                                  f'{info.get("width", "")}×{info.get("height", "")}'])
            elif info:
                lines.extend(['', f'Video: {info.get("video_codec", "")} · {info.get("width", "")}×{info.get("height", "")}',
                              f'Thời lượng: {info.get("duration_seconds", "")} giây'])
            if record.get('proof'):
                lines.append('Xác minh: ' + ('thông số khớp mã băm trong file' if record['proof'] == 'parameter_hash'
                                             else 'mọi khối dữ liệu đúng mã kiểm tra (CRC)')
                             + (' · đã giải mã thử toàn bộ' if record.get('verification') else ''))
            lines.extend(['', f'Bản sửa: {record.get("destination", job.destination)}'])
            h264 = record.get('h264')
            if h264:
                names = {'created': 'Đã tạo', 'skipped_existing': 'Đã có sẵn', 'skipped_already_h264': 'Video gốc đã là H264',
                         'cancelled': 'Đã dừng', 'error': 'Lỗi'}
                lines.append(f'Bản H264: {names.get(h264["status"], h264["status"])}'
                             + (f' · {h264["path"]}' if h264.get('path') else '')
                             + ('\n' + h264['reason'] if h264.get('reason') else ''))
            removal = record.get('source_removed')
            if removal:
                lines.append('File gốc: ' + ('đã chuyển vào Thùng rác' if removal['status'] == 'deleted'
                                             else 'còn giữ · ' + removal['reason']))
            messagebox.showinfo('Chi tiết', '\n'.join(lines), parent=self.root)

        def cancel(self):
            self.stop.set()
            self.stop_button.configure(state='disabled')
            self.message.set('Đang dừng và dọn file tạm…')

        def open_results(self):
            for path in dict.fromkeys(self.output_roots):
                if path.is_dir():
                    os.startfile(str(path))

        def close(self):
            if self.busy:
                self.close_pending = True
                self.cancel()
                return
            self.drop_target.close()
            self.root.destroy()

        def pump(self):
            try:
                while True:
                    kind, payload = self.events.get_nowait()
                    if kind == 'scan':
                        self.jobs, issues = payload
                        self.records = {}
                        self.table.delete(*self.table.get_children())
                        for index, job in enumerate(self.jobs):
                            self.table.insert('', 'end', iid=str(index), values=(job.source.name, 'Chờ xử lý'))
                        self.progress['value'] = 0
                        self.set_busy(False)
                        if not self.quiet_scan:
                            self.message.set('Chọn Bắt đầu phục hồi hoặc Quét trùng MP4.' if self.jobs
                                             else 'Không tìm thấy video hay hình trong lựa chọn này.')
                        if issues:
                            self.message.set(issues[0])
                        auto, stopped = self.auto_next, self.stop.is_set()
                        self.auto_next = False
                        if self.flush_pending():
                            self.auto_next = self.auto_next or (auto and not stopped)
                        elif auto and not stopped and self.auto.get() and self.jobs:
                            self.start()
                        self.refresh_count()
                    elif kind in {'scan_error', 'fatal'}:
                        self.set_busy(False)
                        self.message.set('Không hoàn tất: ' + payload)
                        self.flush_pending(allow_auto=False)
                        self.refresh_count()
                    elif kind == 'working':
                        self.table.item(str(payload), values=(self.jobs[payload].source.name, 'Đang phục hồi…'))
                        self.table.see(str(payload))
                        self.message.set(f'Đang xử lý {payload + 1}/{len(self.jobs)}: {self.jobs[payload].source.name}')
                    elif kind == 'converting':
                        self.table.item(str(payload), values=(self.jobs[payload].source.name, 'Đang tạo bản H264…'))
                    elif kind == 'checking':
                        self.table.item(str(payload), values=(self.jobs[payload].source.name, 'Đang kiểm tra phát…'))
                    elif kind == 'progress':
                        index, fraction = payload
                        self.progress['value'] = 100 * (index + fraction) / max(1, len(self.jobs))
                    elif kind == 'verification':
                        if (self.check.get() or self.delete.get()) and not payload:
                            self.status.set('Không tìm thấy FFmpeg/CapCut: chỉ kiểm tra cấu trúc file, chưa giải mã thử'
                                            + (' · sẽ không xóa file gốc.' if self.delete.get() else '.'))
                        elif payload:
                            self.status.set('Kiểm tra toàn bộ hình và tiếng bằng công cụ có sẵn trên máy.')
                    elif kind == 'result':
                        index, record = payload
                        self.records[index] = record
                        state = record['status']
                        names = {'repaired': 'Đã phục hồi', 'skipped_healthy': 'Đã chuẩn · bỏ qua',
                                 'skipped_existing': 'Đã có bản sửa · bỏ qua', 'unsupported': 'Chưa hỗ trợ',
                                 'cancelled': 'Đã dừng', 'error': 'Không phục hồi được'}
                        tag = 'ok' if state == 'repaired' else ('error' if state in {'error', 'unsupported'} else 'skip')
                        label = names.get(state, state)
                        h264_state = record.get('h264', {}).get('status')
                        if h264_state in {'created', 'skipped_existing'}:
                            label += ' · có bản H264'
                        elif h264_state in {'error', 'cancelled'}:
                            label += ' · H264 lỗi' if h264_state == 'error' else ' · H264 đã dừng'
                        removal = record.get('source_removed', {}).get('status')
                        if removal:
                            label += ' · đã xóa file gốc' if removal == 'deleted' else ' · còn giữ file gốc'
                        self.table.item(str(index), values=(self.jobs[index].source.name, label), tags=(tag,))
                    elif kind == 'duplicate_progress':
                        self.progress['value'] = 100 * min(1.0, max(0.0, payload))
                    elif kind == 'duplicate_log':
                        self.message.set(str(payload))
                    elif kind == 'duplicates_done':
                        self.set_busy(False)
                        groups = payload.get('groups', [])
                        errors = payload.get('errors', [])
                        prefix = 'Đã dừng quét trùng' if payload.get('cancelled') else 'Quét trùng'
                        self.message.set(f'{prefix}: {len(groups)} nhóm · {payload.get("duplicate_files", 0)} bản dư'
                                         + (f' · {len(errors)} file chưa so sánh được.' if errors else '.'))
                        if not payload.get('cancelled'):
                            self.progress['value'] = 100
                        if not self.close_pending:
                            show_duplicate_results(self.root, payload)
                        self.flush_pending(allow_auto=False)
                        self.refresh_count()
                    elif kind == 'done':
                        records, roots, reports, issues = payload
                        self.output_roots = [root for root in roots if root.is_dir()]
                        self.set_busy(False)
                        stopped = self.stop.is_set()
                        repaired = sum(r['status'] == 'repaired' for r in records)
                        errors = sum(r['status'] in {'error', 'unsupported'} for r in records)
                        skipped = sum(r['status'].startswith('skipped') for r in records)
                        h264_made = sum(r.get('h264', {}).get('status') == 'created' for r in records)
                        removals = [r['source_removed']['status'] for r in records if r.get('source_removed')]
                        removed = removals.count('deleted')
                        self.message.set(('Đã dừng' if stopped else 'Hoàn tất')
                                         + f': {repaired} phục hồi · {skipped} bỏ qua · {errors} lỗi/chưa hỗ trợ'
                                         + (f' · {h264_made} bản H264' if h264_made else '')
                                         + (f' · {removed} file gốc vào Thùng rác' if removed else '')
                                         + (f' · {len(removals) - removed} file gốc còn giữ' if len(removals) > removed else '')
                                         + '.')
                        self.open_button.configure(state='normal' if self.output_roots else 'disabled')
                        if not stopped:
                            self.progress['value'] = 100
                        if issues:
                            self.status.set(issues[0])
                        if removed:     # recycled originals must not come back as "not found"
                            self.paths = [path for path in self.paths if os.path.exists(path)]
                        self.batch_done = not stopped
                        self.flush_pending(allow_auto=not stopped)
                        self.refresh_count()
            except queue.Empty:
                pass
            if self.close_pending and not self.busy:
                self.close()
            elif self.root.winfo_exists():
                self.root.after(80, self.pump)

    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    app = App()
    app.root.mainloop()
    return 0


def main():
    parser = argparse.ArgumentParser(description='Phục hồi hàng loạt video và hình CapCut, giữ nguyên bản gốc.')
    parser.add_argument('paths', nargs='*')
    parser.add_argument('--headless', action='store_true', help='Chạy không giao diện')
    parser.add_argument('--output-dir', help='Thư mục kết quả cho chế độ dòng lệnh')
    parser.add_argument('--no-recursive', action='store_true')
    parser.add_argument('--no-verify', action='store_true')
    parser.add_argument('--h264', action='store_true', help='Tạo thêm bản H264 cho video đã phục hồi (mặc định tắt)')
    parser.add_argument('--delete-source', action='store_true',
                        help='Chuyển file gốc vào Thùng rác sau khi bản sửa qua kiểm tra phát (mặc định tắt)')
    parser.add_argument('--report', action='store_true', help='Lưu nhật ký ket_qua_*.json (mặc định tắt)')
    parser.add_argument('--no-auto', action='store_true', help='Giao diện: không tự chạy khi thả / chọn file')
    parser.add_argument('--duplicates', action='store_true', help='Quét MP4 trùng nội dung trong chế độ dòng lệnh')
    args = parser.parse_args()
    return headless(args) if args.headless else gui(args)


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception:
        if '--headless' in sys.argv:
            if sys.stderr:
                traceback.print_exc()
            raise SystemExit(1)
        import tkinter.messagebox as messagebox
        messagebox.showerror('VideoFix', 'Không mở được công cụ.\n\n' + traceback.format_exc()[-1800:])
        raise SystemExit(1)
