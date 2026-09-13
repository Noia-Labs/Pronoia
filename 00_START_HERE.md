# Pronoia 分享版｜同事快速开始

> 这是不含发送者数据的冻结源码快照：没有 API Key、数据库、行情、事件集或历史回测结果。首次启动成功后，请在回测中心导入你有权使用的 CSV/事件数据。ZIP 本身不会自动更新；若从 Git 仓库克隆且配置了 `origin`，启动器才会在工作区无修改时执行安全的 fast-forward 更新。

## 第一次启动

1. 把 ZIP 完整解压到一个普通文件夹，不要直接在压缩包预览里运行。
2. 双击 **`Pronoia 启动器.command`**。
3. 按提示填写：
   - 模型 API URL（例如 DeepSeek、火山方舟等服务的兼容接口地址）；
   - API Key（输入过程隐藏）；
   - 模型名称。
4. 首次运行会安装缺失依赖、构建界面并自动打开浏览器；以后双击同一启动器会直接启动。

模型 API 配置只保存在当前解压目录的 `.env`，文件权限为 `600`，不会进入后续生成的分享 ZIP，也不会打印到启动日志。可填写 DeepSeek、火山方舟以及其他提供兼容接口的模型服务地址。API URL 必须使用 HTTPS；仅本机服务可使用 `http://localhost` 或 `http://127.0.0.1`。

若外部策略或数据接口需要自己的密钥，请在 `.env` 另加 `PRONOIA_STRATEGY_SECRET_*` 或 `PRONOIA_DATA_SECRET_*` 变量，并在界面中按提示引用。平台模型 Key 与团队分享密码不能被外部策略读取。

## 常用入口

- **`Pronoia 启动器.command`**：正常启动；首次运行会要求配置 API（若解压工具弄乱中文文件名，可双击 `START.command`）。
- **`Pronoia API 重设.command`**：更换 API URL、Key 或模型后再启动。
- **`stop.sh`**：停止本机服务。
- **`Pronoia 团队分享.command`**：在可信局域网开启带密码的多人访问。
- **`Pronoia 停止分享.command`**：停止局域网分享。

## 运行要求与数据说明

- macOS，Python 3.10 或更高版本；分享包已带编译后的界面，修改前端源码时才需要 Node.js 18 或更高版本；
- 首次启动会在 `backend/.venv` 创建隔离环境并按锁定清单下载 Python 依赖，同时需要能访问你填写的模型 API；
- 分享包不包含发送者的 API Key、数据库、私有行情、事件集或历史回测结果；
- 同事可在回测中心导入自己有权使用的 CSV 行情或事件数据。

若 macOS 首次阻止运行，可在 Finder 中按住 Control 点击启动器并选择“打开”。更完整的局域网、Docker 与公网部署说明见 `docs/TEAM_SHARING.md`。
