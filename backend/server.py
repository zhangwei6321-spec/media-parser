#!/usr/bin/env python3
"""短视频去水印解析 API — Render 云部署版

抖音解析说明（2026-11 现状）：
  1. www.douyin.com/video/{id} 已是纯客户端渲染的空壳（固定 ~72KB），
     里面没有任何 play_addr / url_list，抓它必然拿到空视频地址。
  2. 唯一稳定可用的是 iesdouyin 分享页 SSR：必须同时满足
       - Cookie: ttwid=<有效值>      （ttwid.bytedance.com/ttwid/union/register 换取）
       - User-Agent: Android 移动 UA （iPhone UA 会被判 new_share_low_version 过滤）
     否则返回的 _ROUTER_DATA 里没有 videoInfoRes。
  3. 分享页给的地址是 playwm（带水印），把 playwm 换成 play 即无水印。
"""
import json, re, os, ssl, threading, time
import urllib.request, urllib.parse, urllib.error
from http.server import HTTPServer, BaseHTTPRequestHandler

PORT = int(os.environ.get('PORT', 3000))

SSL_CTX = ssl.create_default_context()
SSL_CTX.check_hostname = False
SSL_CTX.verify_mode = ssl.CERT_NONE

# 分享页只认移动端 UA；iPhone UA 会被判定 new_share_low_version 而拿不到数据
UA_MOBILE = ('Mozilla/5.0 (Linux; Android 8.0.0; SM-G955U Build/R16NW) '
             'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/116.0.0.0 Mobile Safari/537.36')
UA_DESKTOP = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
              '(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36')

MEDIA_REFERER = 'https://www.douyin.com/'


# ===================== HTTP 工具 =====================

def http_get(url, headers=None, timeout=15, want_bytes=False):
    """GET，返回 (final_url, body_text_or_bytes)。跟随重定向。"""
    req = urllib.request.Request(url, headers=headers or {})
    if not headers or 'User-Agent' not in headers:
        req.add_header('User-Agent', UA_MOBILE)
    try:
        resp = urllib.request.urlopen(req, context=SSL_CTX, timeout=timeout)
        raw = resp.read()
        if want_bytes:
            return resp.geturl(), raw
        return resp.geturl(), raw.decode('utf-8', errors='replace')
    except urllib.error.HTTPError as e:
        return e.headers.get('Location', url), b'' if want_bytes else ''


def clean_url(u):
    if not u:
        return ''
    return u.replace('\\u002F', '/').replace('\\u0026', '&')


def no_watermark(u):
    """playwm -> play，去掉 watermark 参数。"""
    if not u:
        return ''
    u = u.replace('/playwm/', '/play/').replace('playwm', 'play')
    u = re.sub(r'watermark=[^&\s]+', 'watermark=0', u)
    u = re.sub(r'/watermark[^/]*/', '/', u)
    return u


# ===================== ttwid =====================

_ttwid_lock = threading.Lock()
_ttwid_cache = {'value': '', 'ts': 0}
TTWID_TTL = 6 * 3600


def get_ttwid(force=False):
    """换取 douyin 可用的 ttwid（带缓存，避免每次解析都多一次请求）。"""
    with _ttwid_lock:
        if not force and _ttwid_cache['value'] and time.time() - _ttwid_cache['ts'] < TTWID_TTL:
            return _ttwid_cache['value']
        payload = json.dumps({
            'region': 'cn', 'aid': 6383, 'needFid': False,
            'service': 'www.douyin.com',
            'migrate_info': {'ticket': '', 'source': 'node'},
            'cbUrlProtocol': 'https', 'union': True,
        }).encode()
        req = urllib.request.Request(
            'https://ttwid.bytedance.com/ttwid/union/register/',
            data=payload,
            headers={'Content-Type': 'application/json', 'User-Agent': UA_DESKTOP},
        )
        try:
            resp = urllib.request.urlopen(req, context=SSL_CTX, timeout=10)
            for hdr in resp.headers.get_all('Set-Cookie') or []:
                m = re.search(r'ttwid=([^;]+)', hdr)
                if m:
                    _ttwid_cache['value'] = m.group(1)
                    _ttwid_cache['ts'] = time.time()
                    return m.group(1)
        except Exception as e:
            print('[ttwid] 获取失败:', e)
        return _ttwid_cache['value']


# ===================== 抖音 =====================

def extract_aweme_id(raw):
    """从各种形态的链接里取 aweme_id / note id。"""
    pats = [
        r'/video/(\d{15,25})', r'/note/(\d{15,25})', r'/photo/(\d{15,25})',
        r'/share/video/(\d{15,25})', r'/share/note/(\d{15,25})',
        r'modal_id=(\d{15,25})', r'item_ids?=(\d{15,25})',
        r'"aweme_id"\s*:\s*"?(\d{15,25})',
        r'\b(\d{19})\b',
    ]
    for p in pats:
        m = re.search(p, raw)
        if m:
            return m.group(1)
    return ''


def _extract_json_after(marker, text):
    """按花括号配对截取完整 JSON（_ROUTER_DATA 是深层嵌套，非贪婪正则会截断）。"""
    idx = text.find(marker)
    if idx < 0:
        return None
    start = text.find('{', idx + len(marker))
    if start < 0:
        return None
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == '\\':
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == '{':
            depth += 1
        elif ch == '}':
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start:i + 1])
                except Exception:
                    return None
    return None


FILTER_HINTS = {
    'status_self_see': '该作品仅作者自己可见（可能在审核中或已设为私密）',
    'status_deleted': '该作品已被作者删除',
    'status_friend_see': '该作品仅作者好友可见',
    'new_share_low_version': '抖音拒绝了本次请求（分享页版本校验未通过），请稍后重试',
    'story_25_filter': '抖音拒绝了本次请求（分享页版本校验未通过），请稍后重试',
}


def fetch_share_item(aweme_id):
    """抓 iesdouyin 分享页 SSR，返回 item dict。失败抛 ValueError(可读原因)。"""
    if not aweme_id:
        raise ValueError('无法提取视频 ID，请确认链接有效')

    ttwid = get_ttwid()
    if not ttwid:
        raise ValueError('无法建立抖音会话（ttwid 获取失败），请稍后重试')

    url = 'https://www.iesdouyin.com/share/video/%s/' % aweme_id
    headers = {
        'User-Agent': UA_MOBILE,
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        'Accept-Language': 'zh-CN,zh;q=0.9',
        'Cookie': 'ttwid=%s' % ttwid,
        'Referer': 'https://www.douyin.com/?is_from_mobile_home=1&recommend=1',
    }
    _, html = http_get(url, headers, timeout=15)
    if not html:
        raise ValueError('抖音分享页无响应，请稍后重试')

    data = _extract_json_after('_ROUTER_DATA', html)
    if not data:
        raise ValueError('抖音分享页结构异常，请稍后重试')

    loader = data.get('loaderData') or {}
    page = None
    for k, v in loader.items():
        if isinstance(v, dict) and ('videoInfoRes' in v or 'video_(id)/page' in k):
            page = v
            break
    info = (page or {}).get('videoInfoRes') or {}

    items = info.get('item_list') or []
    if items:
        # 命中目标 ID 的优先
        for it in items:
            if str(it.get('aweme_id') or '') == str(aweme_id):
                return it
        return items[0]

    # 没有数据 —— 把 filter_list 的原因翻译成人话
    reasons = []
    for f in info.get('filter_list') or []:
        r = (f.get('filter_reason') or '').strip()
        if r:
            reasons.extend([x for x in r.split('&') if x])
    if reasons:
        hint = next((FILTER_HINTS[r] for r in reasons if r in FILTER_HINTS), None)
        if hint:
            raise ValueError(hint)
        raise ValueError('抖音未返回该作品数据（%s）' % '&'.join(reasons))
    raise ValueError('抖音未返回该作品数据，可能是链接已失效或作品不可见')


def _first_url(node):
    """从 {url_list:[...]} 结构里取第一个可用 URL。"""
    if not isinstance(node, dict):
        return ''
    for u in node.get('url_list') or []:
        if u:
            return clean_url(u)
    return ''


def parse_douyin(share_url):
    final_url, _ = ('', '')
    # 1. 短链 / 分享文案 -> 跟一次重定向拿到真实 URL 里的 ID
    aweme_id = extract_aweme_id(share_url)
    if not aweme_id and ('v.douyin.com' in share_url or 'iesdouyin.com/share' in share_url):
        try:
            final_url, _ = http_get(share_url, {
                'User-Agent': UA_MOBILE,
                'Referer': 'https://www.douyin.com/',
            }, timeout=12)
        except Exception:
            final_url = ''
        aweme_id = extract_aweme_id(final_url + ' ' + share_url)
    if not aweme_id:
        raise ValueError('无法提取视频 ID，请确认链接有效')

    # 2. 分享页 SSR
    item = fetch_share_item(aweme_id)

    video = item.get('video') or {}
    play_addr = video.get('play_addr') or {}
    play_url = no_watermark(_first_url(play_addr))

    # 3. 图文作品
    images = []
    for img in item.get('images') or []:
        u = _first_url(img)
        if u and u not in images:
            images.append(u)

    has_video = bool(play_url)
    if has_video:
        media_type = 'video'
    elif images:
        media_type = 'image'
    else:
        raise ValueError('该作品没有可下载的视频或图片（可能是直播回放或已下架）')

    author = item.get('author') or {}
    music = item.get('music') or {}
    stats = item.get('statistics') or {}

    cover = (_first_url(video.get('cover'))
             or _first_url(video.get('origin_cover'))
             or _first_url(video.get('dynamic_cover')))
    if not cover and images:
        cover = images[0]

    return {
        'platform': 'douyin',
        'videoId': str(item.get('aweme_id') or aweme_id),
        'desc': item.get('desc') or '',
        'videoUrl': play_url,
        'coverUrl': cover,
        'author': {
            'nickname': author.get('nickname') or '',
            'avatar': _first_url(author.get('avatar_thumb')),
            'uid': author.get('sec_uid') or '',
            'uniqueId': author.get('unique_id') or '',
        },
        'music': {'title': music.get('title') or '', 'author': music.get('author') or ''},
        'images': images,
        'type': media_type,
        'duration': int((video.get('duration') or 0) / 1000) if video.get('duration') else 0,
        'createTime': item.get('create_time') or 0,
        'stats': {
            'diggCount': int(stats.get('digg_count') or 0),
            'commentCount': int(stats.get('comment_count') or 0),
            'shareCount': int(stats.get('share_count') or 0),
            'collectCount': int(stats.get('collect_count') or 0),
        },
    }


# ===================== 快手 =====================

def parse_kuaishou(share_url):
    final_url, html = http_get(share_url, {
        'User-Agent': UA_MOBILE,
        'Referer': 'https://www.kuaishou.com/',
    })
    video_id = ''
    for pat in [r'/short-video/([\w]+)', r'/fw/photo/([\w]+)', r'photoId=([\w]+)',
                r'"photoId"\s*:\s*"([\w]+)"', r'"videoId"\s*:\s*"([\w]+)"']:
        m = re.search(pat, final_url + html)
        if m:
            video_id = m.group(1)
            break
    if not video_id:
        raise ValueError('无法提取快手视频 ID，请确认链接有效')

    def grab(node, key):
        return _first_url(node.get(key)) if isinstance(node, dict) else ''

    desc = re.search(r'"caption"\s*:\s*"([^"]*)"', html)
    desc = desc.group(1).replace('\\u002F', '/') if desc else ''
    nickname = re.search(r'"userName"\s*:\s*"([^"]*)"', html)
    nickname = nickname.group(1) if nickname else ''
    avatar = re.search(r'"headUrl"\s*:\s*"([^"]*)"', html)
    avatar = clean_url(avatar.group(1)) if avatar else ''
    cover = re.search(r'"poster"\s*:\s*"([^"]*)"', html)
    cover = clean_url(cover.group(1)) if cover else ''
    play_url = ''
    for pat in [r'"playUrl"\s*:\s*"([^"]*)"', r'"srcNoMark"\s*:\s*"([^"]*)"',
                r'"photoUrl"\s*:\s*"([^"]*)"']:
        m = re.search(pat, html)
        if m:
            play_url = clean_url(m.group(1))
            break

    if not play_url:
        raise ValueError('快手未返回视频地址，可能作品已删除或需要登录')

    return {
        'platform': 'kuaishou', 'videoId': video_id, 'desc': desc,
        'videoUrl': play_url, 'coverUrl': cover,
        'author': {'nickname': nickname, 'avatar': avatar, 'uid': ''},
        'music': {'title': '', 'author': ''},
        'images': [cover] if cover else [],
        'type': 'video',
    }


# ===================== HTTP 处理器 =====================

class APIHandler(BaseHTTPRequestHandler):

    def _set_cors(self):
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type, Range')
        self.send_header('Access-Control-Expose-Headers', 'Content-Range, Content-Length, Accept-Ranges')
        self.send_header('Access-Control-Max-Age', '86400')

    def _json_resp(self, code, data):
        body = json.dumps(data, ensure_ascii=False).encode('utf-8')
        self.send_response(code)
        self._set_cors()
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self._set_cors()
        self.end_headers()

    def do_POST(self):
        if self.path.split('?')[0] != '/api/parse':
            return self._json_resp(404, {'error': 'Not Found'})
        try:
            length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(length) if length else b'{}'
            data = json.loads(body)
            url = (data.get('url') or '').strip()
            if not url:
                return self._json_resp(400, {'error': '请输入视频链接'})

            m = re.search(r'https?://[^\s]+', url)
            clean = m.group(0) if m else url
            clean = clean.rstrip(',;，；。、)）')

            if 'douyin.com' in clean or 'iesdouyin.com' in clean:
                result = parse_douyin(clean)
            elif 'kuaishou.com' in clean or 'chenzhongtech.com' in clean:
                result = parse_kuaishou(clean)
            else:
                return self._json_resp(400, {'error': '仅支持抖音和快手视频链接'})

            if not (result.get('videoUrl') or result.get('images')):
                return self._json_resp(502, {'error': '解析未获得媒体地址，请稍后重试'})
            self._json_resp(200, {'success': True, 'data': result})
        except ValueError as e:
            self._json_resp(422, {'success': False, 'error': str(e)})
        except Exception as e:
            import traceback
            traceback.print_exc()
            self._json_resp(500, {'success': False, 'error': str(e) or '解析失败'})

    def _proxy_media(self, url, download=False, filename='video.mp4'):
        """代理 CDN 媒体：透传 Range，保证 <video> 能拖动进度条。"""
        if not url:
            return self._json_resp(400, {'error': '缺少媒体地址'})
        url = urllib.parse.unquote(url)
        headers = {
            'User-Agent': UA_MOBILE,
            'Referer': MEDIA_REFERER,
            'Accept': '*/*',
        }
        rng = self.headers.get('Range')
        if rng:
            headers['Range'] = rng
        try:
            req = urllib.request.Request(url, headers=headers)
            resp = urllib.request.urlopen(req, context=SSL_CTX, timeout=30)
        except urllib.error.HTTPError as e:
            return self._json_resp(502, {'error': '媒体源返回 %s' % e.code})
        except Exception as e:
            return self._json_resp(502, {'error': '媒体源不可达: %s' % e})

        self.send_response(resp.status if resp.status in (200, 206) else 200)
        self._set_cors()
        ctype = resp.headers.get('Content-Type') or ('video/mp4' if not download else 'application/octet-stream')
        self.send_header('Content-Type', ctype)
        self.send_header('Accept-Ranges', 'bytes')
        for h in ('Content-Length', 'Content-Range', 'Last-Modified', 'ETag'):
            v = resp.headers.get(h)
            if v:
                self.send_header(h, v)
        if download:
            self.send_header('Content-Disposition',
                             'attachment; filename="%s"' % urllib.parse.quote(filename))
        self.end_headers()
        try:
            while True:
                chunk = resp.read(65536)
                if not chunk:
                    break
                self.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass  # 浏览器拖动进度条会主动断开旧连接，属正常现象

    def do_GET(self):
        path = self.path.split('?')[0]
        qs = urllib.parse.urlparse(self.path).query
        params = urllib.parse.parse_qs(qs)
        target = params.get('url', [''])[0]

        if path in ('/', '/health'):
            return self._json_resp(200, {
                'status': 'ok', 'service': 'media-parser-api', 'version': 2,
            })
        if path == '/api/preview/video':
            return self._proxy_media(target)
        if path == '/api/preview/image':
            return self._proxy_media(target)
        if path == '/api/download':
            filename = params.get('filename', ['video.mp4'])[0]
            return self._proxy_media(target, download=True, filename=filename)
        return self._json_resp(404, {'error': 'Not Found'})

    def log_message(self, fmt, *args):
        print('[%s] %s' % (self.log_date_time_string(), args[0]))


if __name__ == '__main__':
    server = HTTPServer(('0.0.0.0', PORT), APIHandler)
    server.allow_reuse_address = True
    print('API 服务已启动: 0.0.0.0:%d' % PORT)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print('\n已停止')
        server.server_close()
