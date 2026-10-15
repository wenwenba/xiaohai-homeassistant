# 小海智家

把「小海智家」账号下的设备接到 Home Assistant。集成走中海私有云 `smarthome-api.coli688.com`，不需要 `local_key`，也不走涂鸦公有云。

当前版本会：

- 用手机号或邮箱登录
- 列出家庭下的设备
- 为每台设备提供在线状态
- 为值为开/关的数据点提供开关

网关本身通常只有在线状态。子设备出现在同一列表里时，会各自成为一台 Home Assistant 设备。

## 安装

1. 打开 HACS → 集成 → 右上角菜单 → 自定义存储库。
2. 仓库填 `https://github.com/wenwenba/xiaohai-homeassistant`，类别选 Integration。
3. 在 HACS 里下载「小海智家」，然后重启 Home Assistant。
4. 设置 → 设备与服务 → 添加集成 → 小海智家。
5. 填写小海 App 的手机号或邮箱、密码。国家码默认 `86`。

密码只填在 Home Assistant 的配置界面里，不要发到聊天或提交到这个仓库。

## 说明

请求签名按小海 2.1.8 的字段规则生成：参与签名的字段排序后用 `||` 连接，`postData` 先做 MD5 再按 App 的方式换位，最后用 App 内置密钥做 HMAC-SHA256。App 自己的最后一步在原生库里。如果配置时提示签名被拒绝，就是这一步和云端不一致，需要继续补签名，而不是再去填涂鸦 IoT 的 Client Secret。
