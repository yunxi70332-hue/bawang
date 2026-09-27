# Flutter AOT 恢复索引（Blutter）

- 原始 `libapp.so`：`E:\霸王茶姬\decompiled\raw_apk\lib\arm64-v8a\libapp.so`
- SHA-256：`8b4aa0dd6dc15ec8380e402bfdb251941678c5b7830ee5a9200822857da5edd7`
- 大小：12911520 bytes
- 说明：输出为 AOT 恢复辅助材料，不等同于原始 Dart 源码。定位到的逻辑应通过原始 SO、Frida 调用栈或脱敏日志交叉验证。

## 关键词统计

- `login`: 2890
- `mobile`: 584
- `refresh`: 456
- `request`: 371
- `response`: 216
- `token`: 167
- `encrypt`: 160
- `api`: 137
- `interceptor`: 76
- `dio`: 74
- `header`: 60
- `baseurl`: 52
- `sign`: 38
- `device`: 14
- `storage`: 13
- `decrypt`: 12
- `secure`: 5

## 优先阅读文件

| 文件 | 命中 |
|---|---:|
| `asm/chagee_cn_login_module/page/phone_login/phone_login_bloc.dart` | login:600, token:9, encrypt:12, sign:2, request:18, response:4, api:8, mobile:24 |
| `asm/chagee_cn_login_module/page/phone_login/phone_login_page.dart` | login:580, header:3, request:8, secure:2, api:6 |
| `asm/chagee_cn_login_module/business/login_service.dart` | login:426, token:37, request:3, api:15, mobile:2 |
| `asm/chagee_cn_login_module/page/change_mobile/change_mobile_bloc.dart` | login:116, encrypt:12, sign:2, request:15, response:4, mobile:225 |
| `asm/chagee_cn_login_module/page/change_mobile/change_mobile_page.dart` | login:87, header:3, request:4, api:4, mobile:248 |
| `asm/chagee_base_network/network/chagee_interceptor.dart` | encrypt:45, decrypt:12, sign:10, header:22, interceptor:70, dio:21, request:27, response:45, baseurl:9 |
| `asm/chagee_cn_login_module/page/verify/verify_bloc.dart` | login:164, token:9, encrypt:16, sign:2, request:26, response:5, api:6, mobile:12 |
| `asm/chagee_base_network/network/chagee_network.dart` | login:3, token:8, header:2, interceptor:6, dio:41, request:67, response:54, baseurl:6 |
| `asm/chagee_cn_login_module/page/bind_phone/bind_phone_page.dart` | login:145, secure:2, api:2 |
| `asm/chagee_cn_login_module/page/verify/verify_page.dart` | login:94, secure:1, api:2 |
| `asm/chagee_cn_login_module/third_part_login/login_third_helper.dart` | login:49, token:9, encrypt:4, request:11, response:5, api:12, mobile:4 |
| `asm/chagee_cn_app_login_module/fastlogin/fast_login.dart` | login:54, token:15, request:3, response:6, api:2 |
| `asm/chagee_cn_login_module/utils/login_page_coordinate.dart` | login:73, request:3, response:1, api:2 |
| `asm/chagee_cn_app_login_module/Interface/login_wrapper_service.dart` | login:70, token:6 |
| `asm/chagee_cn_login_module/net/entities/token_entity.dart` | login:29, token:37 |
| `asm/chagee_cn_login_module/page/bind_phone/bind_phone_bloc.dart` | login:48, encrypt:4, sign:2, request:6, response:1, mobile:2 |
| `asm/chagee_cn_app_order_module/page/order_list/order_list_page.dart` | login:4, header:3, request:2, refresh:46 |
| `asm/chagee_cn_app_login_module/business/login_service.dart` | login:54 |
| `asm/chagee_cn_app_user_module/pages/vouchers_history/vouchers_history/vouchers_history_list_page.dart` | header:3, refresh:46, storage:4 |
| `asm/chagee_cn_login_module/intl_util/login_intl.dart` | login:52 |
| `asm/chagee_cn_app_user_module/util/country_config_utils.dart` | request:2, response:15, api:2, baseurl:32 |
| `asm/chagee_cn_app_user_module/pages/me/me_page.dart` | login:39, request:6, refresh:5 |
| `asm/chagee_cn_app_user_module/pages/my_vouchers/my_vouchers_page.dart` | header:3, refresh:47 |
| `asm/chagee_cn_app_user_module/pages/my_vouchers/applicable_stores/applicable_stores_page.dart` | header:3, refresh:46 |
| `asm/chagee_cn_app_user_module/util/encrypt_utils.dart` | encrypt:29, request:4, api:10, baseurl:5 |
| `asm/chagee_cn_app_order_module/page/order_voucher/order_voucher_page.dart` | header:3, refresh:44 |
| `asm/chagee_cn_app_user_module/pages/my_vouchers/applicable_product/applicable_product_page_content.dart` | header:3, refresh:41, storage:3 |
| `asm/chagee_cn_app_order_module/page/order_detail/order_detail_bloc.dart` | encrypt:4, request:17, response:2, refresh:12, api:4, mobile:4 |
| `asm/chagee_cn_app_user_module/pages/message/message_bloc.dart` | dio:1, request:9, response:7, refresh:16, api:6 |
| `asm/chagee_cn_app_user_module/pages/yuu_rewards/yuu_rewards_bloc.dart` | request:12, response:4, refresh:15, api:8 |
| `asm/chagee_cn_app_user_module/pages/point_list/point_list_page.dart` | header:2, refresh:35 |
| `asm/chagee_cn_app_user_module/util/user_util.dart` | login:6, token:3, encrypt:5, request:6, response:3, device:8, api:2, mobile:2 |
| `asm/chagee_cn_app_order_module/page/order_list/order_list_bloc.dart` | login:2, encrypt:4, request:9, response:2, refresh:11, mobile:4 |
| `asm/chagee_cn_login_module/page/select_country/select_country_page.dart` | login:30, api:2 |
| `asm/chagee_cn_app_order_module/page/order_detail/order_detail_page.dart` | request:2, refresh:29 |
| `asm/chagee_cn_app_login_module/Interface/login_auth_manager.dart` | login:16, sign:12 |
| `asm/chagee_cn_app_login_module/fastlogin/model/token_response_model.dart` | login:4, token:17, response:7 |
| `asm/chagee_cn_app_order_module/page/order_place/order_place_bloc.dart` | encrypt:4, request:12, response:4, api:2, mobile:6 |
| `asm/chagee_cn_app_user_module/business/me_service.dart` | encrypt:15, header:2, request:4, response:4, mobile:1 |
| `asm/chagee_cn_app_user_module/pages/personal/personal_bloc.dart` | encrypt:4, dio:2, request:13, response:5, mobile:2 |

## 下一步验证顺序

1. 先阅读 `chagee_base_network` 的 header、interceptor、network 和 request/response 模型。
2. 再将登录模块的路由和字段名与公开登录前流程的脱敏 runtime log 对照。
3. 对任何认证令牌、设备标识、签名材料只保留脱敏证据；不把真实账号状态或原始敏感值写进仓库。
