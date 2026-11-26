/* ========== 下载管理 ========== */
var Downloader = (function () {
  'use strict';

  /** 非直链媒体走在后端代理上：绕开 CDN 的跨域限制，并带上平台 Referer */
  function getProxyUrl(result) {
    if (result.downloadUrl) return result.downloadUrl;
    if (result.mediaUrl && result.platform && result.platform !== 'video' && result.platform !== 'image') {
      return Parser.backendBase + '/api/download?url=' + encodeURIComponent(result.mediaUrl);
    }
    return null;
  }

  function download(result, onProgress) {
    var url = result.mediaUrl;
    if (!url) return Promise.reject(new Error('无可下载媒体链接'));

    var filename = generateFilename(result);
    var proxy = getProxyUrl(result);

    // 抖音/快手：浏览器无法跨域抓 CDN，交给后端代理下载
    if (proxy && !result.direct) {
      triggerDirectDownload(proxy, filename);
      if (onProgress) onProgress(1, 1);
      return Promise.resolve(filename);
    }

    // 直链：fetch + 进度
    return fetch(url).then(function (response) {
      if (!response.ok) throw new Error('HTTP ' + response.status);
      var contentLength = response.headers.get('content-length');
      var total = contentLength ? parseInt(contentLength, 10) : 0;

      if (total && onProgress && response.body && response.body.getReader) {
        var reader = response.body.getReader();
        var chunks = [];
        var received = 0;
        function pump() {
          return reader.read().then(function (r) {
            if (r.done) {
              triggerDownload(new Blob(chunks), filename);
              return filename;
            }
            chunks.push(r.value);
            received += r.value.length;
            onProgress(received, total);
            return pump();
          });
        }
        return pump();
      }
      return response.blob().then(function (blob) {
        if (onProgress) onProgress(blob.size, blob.size);
        triggerDownload(blob, filename);
        return filename;
      });
    }).catch(function () {
      // 跨域被拦时退回直接跳转
      triggerDirectDownload(url, filename);
      return filename;
    });
  }

  function triggerDownload(blob, filename) {
    var blobUrl = URL.createObjectURL(blob);
    var a = document.createElement('a');
    a.href = blobUrl;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    setTimeout(function () { URL.revokeObjectURL(blobUrl); }, 5000);
  }

  /**
   * 走隐藏 iframe 触发下载：后端返回 Content-Disposition: attachment，
   * 浏览器直接落盘，页面不会被导航走（a[download] 在跨域时会被忽略）。
   * Safari 对 iframe 下载不友好，退回新标签。
   */
  function triggerDirectDownload(url, filename) {
    var ua = navigator.userAgent;
    // iOS 全系、微信内置浏览器对 iframe 下载不友好，直接新标签（attachment 会自动落盘）
    var needWindow = /iP(hone|ad|od)/i.test(ua) || /MicroMessenger/i.test(ua)
      || /^((?!chrome|android|crios|fxios).)*safari/i.test(ua);
    if (needWindow) {
      window.open(url, '_blank', 'noopener');
      return;
    }
    var frame = document.createElement('iframe');
    frame.style.cssText = 'display:none;width:0;height:0;border:0';
    frame.src = url;
    document.body.appendChild(frame);
    setTimeout(function () { frame.remove(); }, 120000);
  }

  function generateFilename(result) {
    var base = (result.title || result.author || 'media').replace(/\s+/g, ' ').trim();
    var clean = base.replace(/[<>:"\/\\|?*#]/g, '_').substring(0, 60) || 'media';
    if (result.type === 'image') return clean + guessImageExt(result.mediaUrl);
    return clean + guessVideoExt(result.mediaUrl);
  }

  function guessImageExt(url) {
    var m = (url || '').match(/\.(jpg|jpeg|png|gif|webp|bmp)/i);
    return m ? '.' + m[1].toLowerCase() : '.jpg';
  }

  function guessVideoExt(url) {
    var m = (url || '').match(/\.(mp4|webm|mov|avi|mkv|flv)/i);
    return m ? '.' + m[1].toLowerCase() : '.mp4';
  }

  return {
    download: download,
    generateFilename: generateFilename,
  };
})();
