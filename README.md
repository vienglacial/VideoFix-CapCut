# VideoFix CapCut

Công cụ Windows phục hồi video và hình (PNG, JPG) CapCut bị bọc lớp BDVE, và quét / xóa MP4 trùng nội dung.
Kéo thả file vào cửa sổ là tự chạy; có tùy chọn chuyển file gốc vào Thùng rác sau khi phục hồi xong.
Hướng dẫn cho người dùng: [HUONG_DAN.txt](HUONG_DAN.txt).

## Chạy từ mã nguồn

```
python src/video_fix.py
```

## Test

`tests/test_v12_features.py` tự tạo dữ liệu, chạy được ngay. Các test còn lại dùng video mẫu không nằm
trong repo: trỏ `VIDEOFIX_FIXTURES` tới thư mục có `canh2-batch-input/` và `unsupported-samples/`
(mặc định là `fixtures/`), và `VIDEOFIX_SHORT_CLIP` tới clip ngắn của `test_sparse_short_clip.py`.
Thiếu mẫu thì test tương ứng tự bỏ qua.

```
cd tests
set VIDEOFIX_FIXTURES=D:\path\to\fixtures
python -m unittest discover -p "test_*.py"
```

## Build EXE

```
pip install pyinstaller
pyinstaller --noconfirm packaging/VideoFix_CapCut.spec
```

Kết quả: `dist/VideoFix_CapCut.exe` (một file, không cần cài đặt).

Phần phân tích BDVE kế thừa dự án MIT CapCut Cache Recover; xem [THIRD_PARTY_LICENSES.txt](THIRD_PARTY_LICENSES.txt).
