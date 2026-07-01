# 🎙️ Audio Translator — Dịch âm thanh real-time bằng Gemini

Ứng dụng Windows dịch âm thanh **real-time** từ bất kỳ tiến trình nào (Chrome, Firefox, Spotify...) sang ngôn ngữ bạn chọn, powered by **Google Gemini Live API**.

![Python](https://img.shields.io/badge/Python-3.10--3.12-blue)
![Platform](https://img.shields.io/badge/Platform-Windows%2010%2F11-green)
![License](https://img.shields.io/badge/License-MIT-yellow)

## ✨ Tính năng

- 🔊 **Capture audio theo process** — chỉ nghe đúng app bạn chọn, không lẫn âm hệ thống
- 🚫 **Chống echo tuyệt đối** — bản dịch phát ra loa không bị dịch lại
- 🌍 **Đa ngôn ngữ** — Tiếng Việt, Anh, Nhật, Hàn, Trung, Pháp, Đức, Tây Ban Nha...
- 🎚️ **Điều chỉnh âm lượng gốc** — nghe song song bản gốc + bản dịch
- 🔑 **Lưu API key** — nhập 1 lần, dùng mãi (lưu local an toàn)
- 🖥️ **GUI đẹp** — giao diện dark mode hiện đại, không cần dòng lệnh

## 📥 Tải về

👉 Tải file **[AudioTranslator.exe](dist/AudioTranslator.exe)** trong thư mục `dist/` — chạy luôn, không cần cài Python.

## 🚀 Hướng dẫn sử dụng

1. **Lấy API key** miễn phí tại [Google AI Studio](https://aistudio.google.com/apikey)
2. Mở **AudioTranslator.exe**
3. Dán API key vào ô và nhấn 💾 để lưu
4. Chọn **nguồn âm thanh** (Chrome, Firefox, Spotify...)
5. Chọn **ngôn ngữ dịch**
6. Nhấn **▶ Bắt đầu**

## 🛠️ Chạy từ source

```bash
# Yêu cầu Python 3.10-3.12
pip install google-genai proc-tap pyaudio numpy psutil

python chrome_translator.py
```

### CLI options

```
--lang <code>              Ngôn ngữ dịch (mặc định: vi)
--original-volume <0-100>  Âm lượng gốc (mặc định: 30)
--pid <int>                Dùng PID cụ thể, bỏ qua bước chọn
--list-audio               Liệt kê process đang phát audio
--cli                      Chạy chế độ dòng lệnh (không GUI)
```

## 📋 Yêu cầu hệ thống

- Windows 10/11 (build 20H1+)
- Loa hoặc headphone
- Kết nối internet
- API key Google Gemini (miễn phí)

## ⚙️ Cách hoạt động

```
Chrome/App phát audio
       │
       ▼ ProcTap capture (per-process, chống echo)
       │
       ├── Âm gốc giữ lại → mixer
       │
       └── Downmix mono 16kHz → Gemini Live API
                                      │
                                      ▼
                              Bản dịch audio 24kHz
                                      │
                                      ▼
                               Mixer → Loa 🔊
                        (dịch 100% + gốc 30%)
```

## 👨‍💻 Tác giả

**@dieptrader** — [Telegram](https://t.me/dieptrader)

## 📄 License

MIT License — tự do sử dụng và chỉnh sửa.
