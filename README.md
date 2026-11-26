# 媒体解析器 — 解析后端

抖音 / 快手 解析后端（Python 标准库，无第三方依赖），为
[book-reader/media-parser](https://zhangwei6321-spec.github.io/book-reader/media-parser/)
提供 `/api/parse`、`/api/preview/video`、`/api/preview/image`、`/api/download`。

Render 服务：https://media-parser-bfmx.onrender.com

## 抖音解析要点（2026-11）

- `www.douyin.com/video/{id}` 已是纯 CSR 空壳，抓 HTML 拿不到 `play_addr`。
- 稳定路径是 `iesdouyin.com/share/video/{id}` 分享页 SSR，前提两条缺一不可：
  1. 带 `Cookie: ttwid=...`（`ttwid.bytedance.com/ttwid/union/register/` 换取，缓存 6h）
  2. `User-Agent` 必须是 Android 移动 UA（iPhone UA 会被 `new_share_low_version` 过滤）
- 分享页给的是 `playwm`（带水印），`playwm → play` 即无水印。
- 无数据时 `videoInfoRes.filter_list[].filter_reason` 会给出原因
  （`status_deleted` / `status_self_see` / `status_friend_see`），后端翻成人话返回。

## 本地运行

```bash
cd backend
python3 server.py          # 默认 3000
```

## 接口

POST `/api/parse`  body: `{"url":"<分享链接或整段分享文案>"}`
