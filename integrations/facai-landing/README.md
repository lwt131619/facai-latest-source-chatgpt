# facai-landing — 对外落地页

生产实例：VPS `/var/www/facai-landing`，由 nginx 直接托管为静态站。

内容：`index.html`（落地页）、`robots.txt`、`sitemap.xml`。

部署：rsync 到 `/var/www/facai-landing` 即可，无需重启服务（nginx 静态读取）。
不含任何服务端逻辑或密钥。
