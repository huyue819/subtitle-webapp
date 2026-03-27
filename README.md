# 自动中文字幕网页版

这是一个可直接部署的单服务版本：
- 前端：静态网页
- 后端：FastAPI
- 核心能力：上传视频、语音识别、翻译成中文、导出 SRT、外挂字幕视频、烧录字幕视频

## 适合谁

适合先做一个自己可用、手机浏览器能访问的网站版本。

## 本地运行

### 1. 安装依赖

```bash
pip install -r requirements.txt
```

### 2. 安装 ffmpeg

macOS:
```bash
brew install ffmpeg
```

Ubuntu/Debian:
```bash
sudo apt update
sudo apt install ffmpeg
```

Windows:
- 下载 ffmpeg
- 把 ffmpeg 和 ffprobe 加入 PATH

### 3. 启动

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

浏览器打开：
```bash
http://localhost:8000
```

## 部署到 Railway

### 最省事做法
1. 把整个项目上传到 GitHub
2. 在 Railway 新建项目并导入该仓库
3. Railway 会自动读取：
   - `requirements.txt`
   - `Procfile`
4. 部署完成后你会拿到一个默认网址，比如：
   - `https://your-project-name.up.railway.app`
5. 手机直接打开这个网址即可

### Railway 里建议加的环境变量
不是必须，但建议：

- `WHISPER_DEVICE=cpu`
- `WHISPER_COMPUTE_TYPE=int8`

## 部署到 Render

1. 新建 Web Service
2. 连接 GitHub 仓库
3. Build Command:

```bash
pip install -r requirements.txt
```

4. Start Command:

```bash
uvicorn app.main:app --host 0.0.0.0 --port $PORT
```

5. 部署后拿到默认 `onrender.com` 地址
6. 手机浏览器直接访问

## 文件结构

```text
subtitle_webapp/
├── app/
│   ├── main.py
│   └── static/
│       └── index.html
├── uploads/
├── outputs/
├── requirements.txt
├── Procfile
├── runtime.txt
└── README.md
```

## 接口说明

### `GET /api/health`
检查服务状态。

### `POST /api/subtitle`
表单字段：
- `file`: 视频文件
- `source_language`: `auto` / `en` / `ja` / `ko` / `zh`
- `mode`: `translate` / `transcribe`
- `output_mode`: `srt` / `softsub` / `hardsub`
- `model_size`: `tiny` / `base` / `small` / `medium`

返回：
- `srt_text`
- `srt_download_url`
- `video_download_url`
- `detected_language`
- `language_probability`

## 手机如何访问

不需要域名。

只要你部署到 Railway 或 Render，平台会给你一个默认网址。把这个网址发到手机上，直接用浏览器打开就行。

## 注意

1. 这个项目适合先做自用版
2. 视频越大，处理越慢
3. 免费额度通常不适合大量用户长期使用
4. 首次访问可能会有冷启动
5. 烧录字幕视频会比只导出 SRT 更吃资源

## 下一步可升级

- 增加任务队列，避免长视频请求超时
- 增加用户登录
- 增加字幕样式设置
- 增加对象存储
- 增加异步处理和进度轮询
