# yett — image chạy trên Linux (host qua Docker Desktop trên Windows/macOS/Linux).
# Chạy trong container = môi trường POSIX → sh/cmdguard/quyền file hoạt động đúng thiết kế.
FROM python:3.11-slim

# Công cụ hay dùng cho tool exec + remote ops (git, ssh client, curl).
RUN apt-get update && apt-get install -y --no-install-recommends \
      git openssh-client ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir -e .

# Thư mục dữ liệu (mount volume khi chạy để giữ qua restart).
RUN mkdir -p /app/state /app/workspace /app/secrets && chmod 700 /app/secrets

# Không chạy bằng root cho an toàn (uid cố định để mount volume ổn định).
RUN useradd -m -u 10001 yett && chown -R yett:yett /app
USER yett

ENTRYPOINT ["yett"]
CMD ["--help"]
