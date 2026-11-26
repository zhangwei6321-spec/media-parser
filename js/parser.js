/* ========== 平台识别与 URL 解析 ========== */
var Parser = (function () {
  'use strict';

  var host = window.location.hostname;
  var isLocal = host === 'localhost' || host === '127.0.0.1' || host === '' || host.indexOf('192.168.') === 0;

  // 本地起后端时走 localhost:3000，线上走 Render
  var BACKEND_BASE = isLocal ? 'http://localhost:3000' : 'https://media-parser-bfmx.onrender.com';

  var PLATFORMS = {
    douyin: {
      name: '抖音', icon: '🎵',
      patterns: [/v\.douyin\.com/i, /douyin\.com/i, /iesdouyin\.com/i],
    },
    kuaishou: {
      name: '快手', icon: '📱',
      patterns: [/kuaishou\.com/i, /chenzhongtech\.com/i, /kuaishou\.app/i],
    },
    image: {
      name: '图片', icon: '🖼️',
      patterns: [/\.(jpg|jpeg|png|gif|webp|bmp|svg)(\?|$)/i],
      directMedia: true,
    },
    video: {
      name: '视频直链', icon: '🎥',
      patterns: [/\.(mp4|webm|mov|avi|mkv|flv|wmv|m3u8)(\?|$)/i],
      directMedia: true,
    },
  };

  function detectPlatform(url) {
    var keys = Object.keys(PLATFORMS);
    for (var i = 0; i < keys.length; i++) {
      var pts = PLATFORMS[keys[i]].patterns;
      for (var j = 0; j < pts.length; j++) {
        if (pts[j].test(url)) return keys[i];
      }
    }
    return 'unknown';
  }

  function isValidUrl(str) {
    try {
      var u = new URL(str);
      return u.protocol === 'http:' || u.protocol === 'https:';
    } catch (_e) { return false; }
  }

  /** 从分享文案里抠出第一条 http(s) 链接 */
  function extractUrls(text) {
    var re = /https?:\/\/[^\s，。；！？、"'）)】]+/g;
    var matches = text.match(re) || [];
    var seen = {};
    var out = [];
    matches.forEach(function (u) {
      var clean = u.replace(/[,;，；。.!！?？'"]+$/, '');
      if (!seen[clean]) { seen[clean] = 1; out.push(clean); }
    });
    return out;
  }

  /** 带超时的 fetch */
  function fetchWithTimeout(url, options, timeoutMs) {
    var controller = new AbortController();
    var timer = setTimeout(function () { controller.abort(); }, timeoutMs || 30000);
    var opts = Object.assign({}, options || {}, { signal: controller.signal });
    return fetch(url, opts).then(function (resp) {
      clearTimeout(timer);
      return resp;
    }).catch(function (e) {
      clearTimeout(timer);
      if (e.name === 'AbortError') throw new Error('timeout');
      throw e;
    });
  }

  /** 云端后端不支持跨域直连 CDN，预览和下载都走后端代理 */
  function proxiedMediaUrl(mediaUrl, kind) {
    if (!mediaUrl) return null;
    var path = kind === 'image' ? '/api/preview/image' : '/api/preview/video';
    return BACKEND_BASE + path + '?url=' + encodeURIComponent(mediaUrl);
  }

  /** 主入口 */
  function parseUrl(url, onStatus) {
    var trimmed = (url || '').trim();
    if (!trimmed) return Promise.reject(new Error('请输入有效链接'));
    if (!isValidUrl(trimmed)) return Promise.reject(new Error('链接格式不正确'));

    var platform = detectPlatform(trimmed);
    var pInfo = PLATFORMS[platform];

    // 直链媒体 — 立刻返回
    if (pInfo && pInfo.directMedia) {
      var title = '';
      try { title = decodeURIComponent(trimmed.split('/').pop().split('?')[0]); } catch (_e) {}
      return Promise.resolve({
        originalUrl: trimmed, platform: platform,
        platformName: pInfo.name, platformIcon: pInfo.icon,
        mediaUrl: trimmed, previewUrl: trimmed, coverUrl: null,
        title: title || '未命名媒体', author: '',
        type: platform === 'image' ? 'image' : 'video', direct: true, isLocal: isLocal,
      });
    }

    if (platform === 'douyin' || platform === 'kuaishou') {
      if (onStatus) onStatus(isLocal ? '连接本地后端...' : '连接云端后端...');
      return parseViaBackend(trimmed, platform, onStatus);
    }

    if (onStatus) onStatus('通用网页解析...');
    return parseGenericPage(trimmed);
  }

  function parseViaBackend(url, platform, onStatus) {
    return fetchWithTimeout(BACKEND_BASE + '/api/parse', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ url: url }),
    }).then(function (resp) {
      return resp.text().then(function (text) {
        var json = null;
        try { json = JSON.parse(text); } catch (_e) {}
        if (!json) {
          throw new Error('后端返回非 JSON（HTTP ' + resp.status + '），服务可能正在冷启动，请重试');
        }
        if (!resp.ok || !json.success || !json.data) {
          throw new Error(json.error || json.message || ('解析失败（HTTP ' + resp.status + '）'));
        }
        return json.data;
      });
    }).then(function (d) {
      var info = PLATFORMS[platform];
      var isImageSet = d.type === 'image' && (d.images || []).length > 0;
      var mediaUrl = d.videoUrl || d.video_url || d.play_url || d.url || '';
      var type = isImageSet ? 'image' : 'video';

      if (type === 'image' && !mediaUrl) mediaUrl = d.images[0];
      if (!mediaUrl) throw new Error('未获取到媒体地址，请稍后重试');

      var images = (d.images || []).filter(Boolean);
      return {
        originalUrl: url, platform: platform,
        platformName: info.name, platformIcon: info.icon,
        mediaUrl: mediaUrl,
        previewUrl: proxiedMediaUrl(mediaUrl, type),
        downloadUrl: BACKEND_BASE + '/api/download?url=' + encodeURIComponent(mediaUrl),
        coverUrl: d.coverUrl || d.cover_url || null,
        title: d.desc || d.title || '',
        author: (d.author && (d.author.nickname || d.author.uniqueId)) || d.nickname || '',
        music: (d.music && d.music.title) || '',
        duration: d.duration || 0,
        images: images,
        type: type,
        direct: false,
        isLocal: isLocal,
      };
    }).catch(function (e) {
      if (e.message === 'timeout') {
        throw new Error('后端响应超时（免费实例冷启动约需 30 秒），请稍后重试');
      }
      if (e instanceof TypeError && String(e.message).indexOf('fetch') >= 0) {
        throw new Error('无法连接解析后端，请检查网络');
      }
      throw e;
    });
  }

  function parseGenericPage(url) {
    var proxyUrl = 'https://api.allorigins.win/raw?url=' + encodeURIComponent(url);
    return fetchWithTimeout(proxyUrl, {}, 15000).then(function (resp) {
      return resp.text();
    }).then(function (html) {
      function getMeta(prop) {
        var re1 = new RegExp('<meta[^>]+(?:property|name)=["\']' + prop + '["\'][^>]+content=["\']([^"\']+)', 'i');
        var re2 = new RegExp('<meta[^>]+content=["\']([^"\']+)["\'][^>]+(?:property|name)=["\']' + prop + '["\']', 'i');
        var m = html.match(re1) || html.match(re2);
        return m ? m[1] : null;
      }
      var ogVideo = getMeta('og:video') || getMeta('og:video:url');
      var ogImage = getMeta('og:image') || getMeta('og:image:url');
      if (ogVideo) {
        return {
          originalUrl: url, platform: 'video', platformName: '视频', platformIcon: '🎥',
          mediaUrl: ogVideo, previewUrl: ogVideo, coverUrl: ogImage,
          title: getMeta('og:title') || '网页视频', author: '', type: 'video', direct: true, isLocal: false,
        };
      }
      if (ogImage) {
        return {
          originalUrl: url, platform: 'image', platformName: '图片', platformIcon: '🖼️',
          mediaUrl: ogImage, previewUrl: ogImage, coverUrl: null,
          title: getMeta('og:title') || '网页图片', author: '', type: 'image', direct: true, isLocal: false,
        };
      }
      throw new Error('未检测到媒体');
    }).catch(function (e) {
      if (e.message.indexOf('未检测到') >= 0) throw e;
      throw new Error('解析失败：' + e.message);
    });
  }

  function parseBatch(text, onProgress) {
    var urls = extractUrls(text);
    if (!urls.length) return Promise.reject(new Error('未检测到有效链接'));

    var results = [], errors = [];
    function next(i) {
      if (i >= urls.length) return Promise.resolve({ results: results, errors: errors });
      if (onProgress) onProgress(i, urls.length, urls[i]);
      return parseUrl(urls[i]).then(function (r) {
        results.push(r);
        return next(i + 1);
      }).catch(function (e) {
        errors.push({ url: urls[i], error: e.message });
        return next(i + 1);
      });
    }
    return next(0);
  }

  return {
    detectPlatform: detectPlatform,
    parseUrl: parseUrl,
    parseBatch: parseBatch,
    extractUrls: extractUrls,
    isValidUrl: isValidUrl,
    isLocal: isLocal,
    backendBase: BACKEND_BASE,
  };
})();
