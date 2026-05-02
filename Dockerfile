FROM python:3.11-slim

# Enable the "non-free" repository in Debian to access the official unrar package
RUN sed -i 's/Components: main/Components: main non-free/g' /etc/apt/sources.list.d/debian.sources 2>/dev/null || \
    sed -i 's/ main$/ main non-free/g' /etc/apt/sources.list

# Install unrar, curl, and unzip (needed for rclone and CBR extraction)
RUN apt-get update && apt-get install -y \
    unrar \
    curl \
    unzip \
    && rm -rf /var/lib/apt/lists/*

# Install rclone
RUN curl https://rclone.org/install.sh | bash

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Create directories for rclone config and file downloads
RUN mkdir -p /root/.config/rclone downloads

COPY . .

# Default Northflank port
EXPOSE 8080

CMD ["python", "bot.py"]
