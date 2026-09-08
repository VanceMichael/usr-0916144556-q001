# 古籍数字化异文汇校服务 scaffold

This repository is an intentionally incomplete starting point for a pure backend service. It contains input contracts, deterministic fixtures, and a Docker-based scaffold validator; no requested business API is implemented.

Business theme: 四库全书数字化成果展让古籍走出故纸堆
Theme source: https://www.chinanews.com/scroll-news/news1.html
Required stack: Python 3.12, SQLite, Docker Compose

Validate the baseline inputs with:

```sh
docker compose run --rm --no-deps scaffold-check
```

The implementation must preserve the contracts and fixtures, add the service and its automated tests, and provide a repeatable Docker-based black-box self-test. External production systems must not be used.
