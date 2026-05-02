FROM python:3.11-slim

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
