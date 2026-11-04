# 第三方来源与许可

## 原生发送桥

来源：[Gusion1017/WeChat-ChatBot-Mac](https://github.com/Gusion1017/WeChat-ChatBot-Mac)，固定 commit `9094de7859618a8124f9d3162e63c87b0c8c837a`。

桥部分遵循 GPL-3.0。完整许可及上游声明保留于 [LICENSE](integrations/gusion/LICENSE) 和 [NOTICE](integrations/gusion/NOTICE)；NOTICE 是上游原文，描述完整 bridge 源码组成。桥进一步复用 [yincongcyincong/weixin-macos](https://github.com/yincongcyincong/weixin-macos) 的 onebot，具体版本与作者信息见该声明。

本仓库通过本机 HTTP 与独立 Go 桥通信，没有复制上游 SwiftUI AI 应用、模型、TTS 或对外回调业务。适配范围：

- 增加鉴权的状态、群目录和文字提交接口；按账号、桥进程和请求编号核对。
- 仅启用通知所需的文本 R3 和已授权群目录 Hook，跳过入站聊天与媒体 Hook。
- 群目录只返回群名和群 ID，不返回好友或群成员。
- 仅回环监听，Gadget 使用独立 Token；私密缓存留在本机。
- 不确定结果不自动重发，日志隐藏凭证并限制轮转大小。

`scripts/prepare_native_bridge.py` 可从固定上游版本重现修改；差异保存在 `integrations/gusion/upstream.patch`。新增的 `notifier.go`、测试和 `contact_compat.js` 使用兼容的 GPL-3.0 许可。

`scripts/package_native_bridge.py` 同时输出运行归档及该构建对应的修改后源码。公开 Git 仓库不包含二进制构建缓存或运行资料；另行分发桥二进制时应一并提供对应源码、许可证和上游声明。

## 构建依赖

上游构建脚本固定 Go 1.25.14、Frida core / Gadget 17.8.0，并校验下载哈希。Go 模块锁定记录在上游 `bridge/go.mod`、`bridge/go.sum`。依赖保留各自许可证，具体列表见上游 NOTICE；本仓库不随源码发布工具链或微信客户端。

Gadget 的本机鉴权依据 [Frida 官方文档](https://frida.re/docs/gadget/)，Go 桥使用固定 frida-go SDK 的 `RemoteDeviceOptions.SetToken`。

## 系统指令兼容

`contact_compat.js` 处理部分 macOS arm64e.x1 上以 `PACIBSPPC`（`0xdac1a7fe`）开头的 CommonCrypto 入口。代码保留认证指令在原 PC 执行，仅在其后安装入口回调，拒绝 onLeave；普通入口继续使用原拦截路径。

指令语义参考 [Arm 官方说明](https://documentation-service.arm.com/static/67e40f3398aa3c3b6eea6a85)。兼容逻辑用隔离进程和合成零值输入验证，未关闭系统保护；Node 合成保护检查位于 `tests/native_contact_hooks.js`。这不代表任意 macOS / Frida 版本均可兼容。

## 通知应用与历史视觉代码

FastAPI 使用 MIT，Uvicorn 使用 BSD，具体版本见 `requirements.lock.txt`；其余依赖保留各自许可。

旧视觉实现参考过 [send-wechat-message](https://github.com/tonyshield/send-wechat-message) 的操作思路和 [bgclick 技术分析](https://github.com/Lakr233/bgclick-rev-skill/blob/main/bgclick-rev-skill.md)，没有复制其脚本或安装其工具。旧视觉路径不是默认发送后端。
