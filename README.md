# 电商自动剪辑 Web Demo

这是可部署到 Render 的网页版本。

## 本地运行

```powershell
cd /d E:\test\3\web_render_app
python -m pip install -r requirements.txt
python app.py
```

打开：

```text
http://127.0.0.1:5000
```

## Render 部署

1. 把 `web_render_app` 里的文件上传到 GitHub 仓库。
2. Render 新建 Web Service，连接该仓库。
3. Build Command:

```bash
pip install -r requirements.txt
```

4. Start Command:

```bash
gunicorn app:app --timeout 1800 --workers 1 --threads 4
```

## 使用方式

网页里选择：

- 商品素材文件夹：选择整包素材目录，例如 `A鞋`
- BGM 文件：选择一个音频文件
- 口播文案：直接填写或粘贴

点击生成后，服务器会处理并提供 `final.mp4` 下载。

注意：浏览器不能直接读取朋友电脑上的本地路径，所以必须让朋友通过网页选择并上传素材。
