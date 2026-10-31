# VideoFix CapCut

Công cụ Windows phục hồi video CapCut bị bọc lớp BDVE, và quét / xóa MP4 trùng nội dung.
Hướng dẫn cho người dùng: [HUONG_DAN.txt](HUONG_DAN.txt).

## Chạy từ mã nguồn

```
python src/video_fix.py
```

## Test

Các test dùng video mẫu, không nằm trong repo. Trỏ `VIDEOFIX_FIXTURES` tới thư mục có
`canh2-batch-input/` và `unsupported-samples/` (mặc định là `fixtures/`):

```
cd tests
set VIDEOFIX_FIXTURES=D:\path\to\fixtures
python -m unittest discover -p "test_*.py"
```

## Build EXE

```
pip install pyinstaller
pyinstaller --noconfirm packaging/VideoFix_CapCut_v1_1.spec
```

Phần phân tích BDVE kế thừa dự án MIT CapCut Cache Recover; xem [THIRD_PARTY_LICENSES.txt](THIRD_PARTY_LICENSES.txt).
