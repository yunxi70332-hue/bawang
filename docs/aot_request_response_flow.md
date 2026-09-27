# Flutter AOT 请求/响应字段处理：静态证据笔记

- 样本：`libapp.so`
- SHA-256：`8b4aa0dd6dc15ec8380e402bfdb251941678c5b7830ee5a9200822857da5edd7`
- 证据范围：Blutter 产生的伪 Dart/ARM64 注释；没有读取真实请求、密钥、令牌或个人数据。

## 请求侧

- `_handleRequestPostBody` — RVA `0xb48518`, size `0x888`：读取请求扩展配置中的 `requestEncryptFields`、`sk`、`signFields`、`secretKey` 与 `sign` 标记；静态调用标记含 `base64Decode`、`Key`、`Encrypted.fromUtf8`、`AES`，随后调入 `buildEncryptedDataFromOriginal`。
- `buildEncryptedDataFromOriginal` — RVA `0xb49234`, size `0x59c`：存在 Map/List 递归与 `Encrypter::encrypt` / `Encrypted::base64` 调用标记。因此可把它标为“按字段选择、递归转换、输出 Base64 文本”的客户端处理函数；具体输入选择与参数仍须运行时确认。

## 响应侧

- `_decryptResponseIfNeeded` — RVA `0xb21730`, size `0x404`：读取 `responseEncryptFields` 与 `sk`，并存在 `base64Decode`、`Key`、`Encrypted.fromUtf8`、`AES` 标记，再调用 `decryptFieldsInResponse`。
- `decryptFieldsInResponse` — RVA `0xb21ba8`, size `0x554`：存在 Map/List 遍历、递归回调与 `Encrypter::decrypt64` 标记，符合“仅解密配置字段、并深入嵌套对象”的结构。

## 传输配置

- `_handleRequestHeaders` — RVA `0xb49ad4`, size `0x1b0`：读取 `customHeaders` 并调用 `NetHeaderService::headers`。
- `_handleCustomBaseUrl` — RVA `0xb49fa8`, size `0xc4`：读取 `customBaseUrl` 并设置请求 `baseUrl`。

## 结论边界

这些结果只证明 APK 内存在上述客户端代码路径与字段名称；它们不证明固定算法参数、密钥来源、服务端校验规则或任何服务端接受条件。下一步应当是用脱敏的未登录启动日志验证调用次序，而不是保存真实业务请求。
