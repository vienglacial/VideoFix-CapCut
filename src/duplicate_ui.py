"""Duplicate results window with optional move-to-Recycle-Bin cleanup."""
from pathlib import Path
import json
import os
import subprocess
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

from duplicate_delete import delete_duplicates


def readable_size(number):
    number = float(number or 0)
    for unit in ('B', 'KB', 'MB', 'GB', 'TB'):
        if number < 1024 or unit == 'TB':
            return f'{number:.1f} {unit}'
        number /= 1024


def show_duplicate_results(parent, report):
    window = tk.Toplevel(parent)
    window.title('VideoFix 1.1 · MP4 trùng nội dung')
    window.geometry('1100x660')
    window.minsize(800, 500)
    window.configure(bg='#f3f6fb')
    panel = ttk.Frame(window, padding=20)
    panel.pack(fill='both', expand=True)
    groups = report.get('groups', [])
    errors = report.get('errors', [])
    duplicate_count = report.get('duplicate_files', sum(max(0, len(g['files']) - 1) for g in groups))
    suffix = ' · Kết quả một phần vì đã dừng' if report.get('cancelled') else ''
    tk.Label(panel, text=f'{len(groups)} nhóm trùng · {duplicate_count} bản dư' + suffix,
             bg='#f3f6fb', fg='#0f172a', font=('Segoe UI', 17, 'bold'), anchor='w').pack(fill='x')
    tk.Label(panel, text='So sánh nội dung, không dựa vào tên file. Dòng ☑ Xóa sẽ được chuyển vào Thùng rác khi bấm Xóa; '
                         'nhấp đúp hoặc phím cách để đổi Xóa/Giữ.',
             bg='#f3f6fb', fg='#526277', anchor='w').pack(fill='x', pady=(5, 10))
    tk.Label(panel, text=f'Dung lượng các bản dư: {readable_size(report.get("reclaimable_bytes", 0))}'
             + (f' · {len(errors)} file không so sánh được' if errors else ''),
             bg='#f3f6fb', fg='#526277', anchor='w').pack(fill='x', pady=(0, 10))
    bottom = ttk.Frame(panel)
    bottom.pack(side='bottom', fill='x', pady=(12, 0))
    detail = tk.StringVar(value='Chọn một file để xem đường dẫn hoặc mở vị trí của nó.')
    tk.Label(bottom, textvariable=detail, bg='#f3f6fb', fg='#526277',
             anchor='w', justify='left', wraplength=1030).pack(fill='x', pady=(0, 10))
    actions = ttk.Frame(bottom)
    actions.pack(fill='x')
    frame = ttk.Frame(panel)
    frame.pack(fill='both', expand=True)
    table = ttk.Treeview(frame, columns=('role', 'size', 'path'), show='tree headings')
    table.heading('#0', text='Nhóm / Video')
    table.heading('role', text='Xử lý')
    table.heading('size', text='Dung lượng')
    table.heading('path', text='Thư mục')
    table.column('#0', width=360, minwidth=200)
    table.column('role', width=110, minwidth=90, stretch=False)
    table.column('size', width=105, minwidth=90, stretch=False)
    table.column('path', width=440, minwidth=220)
    vertical = ttk.Scrollbar(frame, orient='vertical', command=table.yview)
    horizontal = ttk.Scrollbar(frame, orient='horizontal', command=table.xview)
    table.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
    horizontal.pack(side='bottom', fill='x')
    vertical.pack(side='right', fill='y')
    table.pack(side='left', fill='both', expand=True)
    table.tag_configure('keep', foreground='#087443')
    table.tag_configure('error', foreground='#b42318')
    table.tag_configure('delete', foreground='#b42318')
    table.tag_configure('deleted', foreground='#98a2b3')
    paths_by_row = {}
    file_rows = {}  # row -> (group index, file entry)
    keepers = {}    # group index -> path of the copy that is never deleted
    marked = set()  # rows to move to the Recycle Bin
    deleted = set()

    def refresh(row):
        index, file = file_rows[row]
        if row in deleted:
            role, tags = 'Đã xóa', ('deleted',)
        elif file['path'] == keepers[index]:
            role, tags = 'Giữ lại', ('keep',)
        elif row in marked:
            role, tags = '☑ Xóa', ('delete',)
        else:
            role, tags = '☐ Giữ', ()
        table.item(row, values=(role,) + tuple(table.item(row, 'values')[1:]), tags=tags)

    for number, group in enumerate(groups, 1):
        kind = 'Trùng toàn bộ file' if group.get('kind') == 'exact' else 'Trùng dữ liệu hình/tiếng'
        parent_row = table.insert('', 'end', text=f'Nhóm {number} · {kind}', open=True)
        keepers[number - 1] = str(group.get('keep', group.get('files', [{}])[0].get('path', '')))
        for file in group['files']:
            path = Path(file['path'])
            row = table.insert(parent_row, 'end', text=path.name,
                values=('', readable_size(file.get('bytes', 0)), str(path.parent)))
            paths_by_row[row] = path
            file_rows[row] = (number - 1, file)
            if file['path'] != keepers[number - 1]:
                marked.add(row)
            refresh(row)
    if errors:
        error_parent = table.insert('', 'end', text=f'{len(errors)} file không so sánh được', open=True, tags=('error',))
        for entry in errors:
            if isinstance(entry, dict):
                path = Path(entry.get('path', entry.get('source', '')))
                reason = entry.get('reason', entry.get('error', 'Không đọc được file'))
            else:
                path, reason = Path('.'), str(entry)
            row = table.insert(error_parent, 'end', text=path.name,
                               values=('Chưa kiểm tra', '', reason), tags=('error',))
            paths_by_row[row] = path
    if not groups and not errors:
        table.insert('', 'end', text='Không tìm thấy MP4 trùng nội dung trong các file đã quét.')

    def selected_path():
        selected = table.selection()
        return paths_by_row.get(selected[0]) if selected else None

    def select(event=None):
        path = selected_path()
        if path:
            detail.set(str(path))

    def update_summary():
        rows = marked - deleted
        size = sum(file_rows[row][1].get('bytes', 0) for row in rows)
        delete_button.configure(text=f'Xóa {len(rows)} bản trùng ({readable_size(size)})…',
                                state='normal' if rows else 'disabled')

    def toggle(event=None):
        for row in table.selection():
            if row not in file_rows or row in deleted:
                continue
            index, file = file_rows[row]
            if file['path'] == keepers[index]:
                detail.set('Đây là bản giữ lại. Chọn file khác rồi bấm "Chọn làm bản giữ" để đổi.')
                continue
            marked.symmetric_difference_update({row})
            refresh(row)
        update_summary()
        return 'break'

    def choose_keeper():
        selected = table.selection()
        row = selected[0] if selected else None
        if row not in file_rows or row in deleted:
            detail.set('Chọn một file chưa xóa trong nhóm để giữ lại.')
            return
        index, file = file_rows[row]
        if file['path'] == keepers[index]:
            return
        old = next(r for r, (i, f) in file_rows.items() if i == index and f['path'] == keepers[index])
        keepers[index] = file['path']
        marked.discard(row)
        if old not in deleted:
            marked.add(old)
        refresh(row)
        refresh(old)
        update_summary()

    def delete_marked():
        rows = [row for row in file_rows if row in marked and row not in deleted]
        if not rows:
            return
        size = sum(file_rows[row][1].get('bytes', 0) for row in rows)
        if not messagebox.askyesno('VideoFix',
                f'Chuyển {len(rows)} bản trùng ({readable_size(size)}) vào Thùng rác?\n\n'
                'Mỗi nhóm vẫn còn bản "Giữ lại". File nào đã thay đổi sau khi quét sẽ được bỏ qua.\n'
                'Có thể khôi phục từ Thùng rác nếu cần.', icon='warning', parent=window):
            return
        by_group = {}
        for row in rows:
            by_group.setdefault(file_rows[row][0], []).append(row)
        outcomes = []
        window.configure(cursor='watch')
        window.update_idletasks()
        try:
            for index, group_rows in by_group.items():
                results = delete_duplicates(groups[index], keepers[index],
                    [file_rows[row][1]['path'] for row in group_rows], hwnd=window.winfo_id())
                outcomes += zip(group_rows, results)
        finally:
            window.configure(cursor='')
        removed, failed = 0, []
        for row, outcome in outcomes:
            file = file_rows[row][1]
            file['deleted'] = outcome['status'] == 'deleted'
            if file['deleted']:
                deleted.add(row)
                removed += file.get('bytes', 0)
            else:
                failed.append(outcome)
            refresh(row)
        report.setdefault('deletions', []).extend(outcome for _, outcome in outcomes)
        update_summary()
        done = len(outcomes) - len(failed)
        detail.set(f'Đã chuyển {done} file ({readable_size(removed)}) vào Thùng rác.'
                   + (f' {len(failed)} file không xóa.' if failed else ''))
        if failed:
            messagebox.showwarning('VideoFix', f'Đã xóa {done} file. Không xóa {len(failed)} file:\n\n'
                + '\n'.join(f'• {Path(o["path"]).name}: {o["reason"]}' for o in failed[:12])
                + ('\n…' if len(failed) > 12 else ''), parent=window)

    def open_location():
        path = selected_path()
        if not path or not path.exists():
            return
        try:
            subprocess.Popen(['explorer.exe', '/select,', str(path)])
        except OSError as error:
            messagebox.showerror('VideoFix', str(error), parent=window)

    def export():
        filename = filedialog.asksaveasfilename(parent=window, title='Lưu danh sách MP4 trùng',
            defaultextension='.txt', initialfile='MP4_trung_noi_dung.txt',
            filetypes=[('Văn bản', '*.txt'), ('JSON', '*.json')])
        if not filename:
            return
        try:
            target = Path(filename)
            if target.suffix.casefold() == '.json':
                text = json.dumps(report, ensure_ascii=False, indent=2, default=str)
            else:
                removed = sum(bool(f.get('deleted')) for g in groups for f in g['files'])
                lines = ['MP4 TRÙNG NỘI DUNG', f'{len(groups)} nhóm; {duplicate_count} bản dư.',
                         f'Đã chuyển {removed} file vào Thùng rác.', '']
                for number, group in enumerate(groups, 1):
                    lines.append(f'Nhóm {number} ({group.get("kind", "media")}):')
                    keep = keepers[number - 1]
                    for file in group['files']:
                        role = ('ĐÃ XÓA' if file.get('deleted') else
                                'GIỮ LẠI' if str(file['path']) == keep else 'BẢN TRÙNG')
                        lines.append(f'  [{role}] {file["path"]} ({readable_size(file.get("bytes", 0))})')
                    lines.append('')
                if errors:
                    lines.extend(['FILE KHÔNG SO SÁNH ĐƯỢC', json.dumps(errors, ensure_ascii=False, indent=2)])
                text = '\n'.join(lines)
            target.write_text(text, encoding='utf-8-sig')
            detail.set('Đã lưu danh sách: ' + str(target))
        except OSError as error:
            messagebox.showerror('VideoFix', str(error), parent=window)

    table.bind('<<TreeviewSelect>>', select)
    table.bind('<Double-1>', toggle)
    table.bind('<space>', toggle)
    ttk.Button(actions, text='Mở vị trí file', command=open_location).pack(side='left')
    ttk.Button(actions, text='Chọn làm bản giữ', command=choose_keeper).pack(side='left', padx=(8, 0))
    ttk.Button(actions, text='Lưu danh sách…', command=export).pack(side='left', padx=8)
    ttk.Button(actions, text='Đóng', command=window.destroy).pack(side='right')
    delete_button = ttk.Button(actions, command=delete_marked)
    delete_button.pack(side='right', padx=8)
    update_summary()
    return window
