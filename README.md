# Stream Editor

Локальный веб-интерфейс для транскрипции длинных записей, отбора хайлайтов и подготовки чернового монтажа в Adobe Premiere Pro.

Приложение показывает видео и транскрипт с таймкодами, позволяет редактировать CSV с моментами, экспортирует FCP7 XML и создаёт обзорный MP4. Исходное видео остаётся на компьютере пользователя. Для транскрипции используются MLX Whisper на Mac с Apple Silicon или Faster Whisper на CPU. Модель загружается при первом запуске. OpenAI API не требуется.

## Быстрый запуск

Нужны Python 3.12, `ffmpeg` и `ffprobe` в `PATH`.

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements-desktop.txt
.venv/bin/python app.py
```

На Windows используйте `.venv\Scripts\python.exe` вместо `.venv/bin/python`. Откройте `http://127.0.0.1:8765`, если браузер не открылся сам.

Для MLX на Apple Silicon дополнительно установите `mlx-whisper` в то же окружение и выберите движок **MLX**. На Windows и Linux Faster Whisper работает на CPU. Для FFmpeg: `brew install ffmpeg` на macOS, `winget install Gyan.FFmpeg` на Windows или системный пакет `ffmpeg` на Linux.

## Linux и macOS через Docker

Для локальной сборки скопируйте [`.env.example`](.env.example) в `.env` и при необходимости измените порт. Docker Compose читает этот файл автоматически.

```bash
cp .env.example .env
docker compose up --build -d
```

Откройте `http://127.0.0.1:8765` (или порт из `.env`). Положите видео в `workspace/` и укажите путь внутри контейнера, например `/data/stream.mp4`. Результаты появятся в `workspace/stream-editor/`. Порт доступен только с локального компьютера. На macOS нужен Docker Desktop. Контейнер использует Faster Whisper на CPU; для MLX запускайте приложение непосредственно на macOS.

## Работа с проектом

1. Загрузите видео через браузер. При необходимости настройте аудиодорожку и движок транскрипции, затем сохраните проект. Можно также указать абсолютный путь к видео на том же компьютере.
2. Запустите транскрипцию. Повторный запуск продолжает готовые части. Для другого исходника выберите новую папку транскрипта.
3. Добавьте моменты или укажите существующий CSV с колонками `start,end,category,score,reason,needs_video_review`. Таймкоды: `HH:MM:SS`.
4. Экспортируйте XML для Premiere или обзорный MP4. Проверьте монтаж и синхронизацию в Premiere перед публикацией.

Интерфейс не выбирает хайлайты автоматически: решения по содержанию и проверка видеоряда остаются за редактором. Черновой XML использует номинальную частоту кадров, поэтому для исходников с переменным FPS нужно проверить границы и звук.

Командные утилиты можно запускать отдельно:

```bash
python transcribe_stream.py /path/to/video.mp4 --work-dir /path/to/transcript
python highlights_to_fcp7xml.py /path/to/video.mp4 /path/to/highlights.csv /path/to/output.xml
python render_preview.py /path/to/video.mp4 /path/to/highlights.csv /path/to/preview.mp4
```

Образец формата CSV: [examples/highlights.csv](examples/highlights.csv). Примеры запросов для ручного анализа транскрипта: [PROMPTS.md](PROMPTS.md).

## Файлы релиза

При создании тега `v*` или публикации Release для такого тега [GitHub Actions](.github/workflows/installers.yml) прикрепляет к релизу `StreamEditor.exe` для Windows и два контейнерных образа: `StreamEditor-Linux-amd64.docker.tar.gz` для Linux x86-64 и Mac с Intel, `StreamEditor-Linux-arm64.docker.tar.gz` для Linux ARM64 и Mac с Apple Silicon. Это Linux-контейнеры, запускаемые на macOS через Docker Desktop.

Загрузите архив для архитектуры компьютера и запустите образ:

```bash
docker load -i StreamEditor-Linux-amd64.docker.tar.gz
mkdir -p workspace
docker run -d --name stream-editor -p 127.0.0.1:8765:8765 -v "$(pwd)/workspace:/data" stream-editor:release
```

На ARM64 замените имя архива на `StreamEditor-Linux-arm64.docker.tar.gz`. Откройте `http://127.0.0.1:8765`; файлы приложения сохраняются в `workspace/stream-editor/`. Для Windows нужен FFmpeg в `PATH`. Windows `.exe` пока не подписан. Ручной запуск workflow сохраняет файлы как артефакты Actions без создания релиза.

## Разработка и публикация

```bash
python -m unittest discover -s tests -v
python -m py_compile app.py transcribe_stream.py highlights_to_fcp7xml.py render_preview.py
```

Медиа, транскрипты, экспорты, локальный виртуальный Python и `.env` исключены через `.gitignore`; образец настроек хранится в [`.env.example`](.env.example). В репозитории остаются исходный код, Docker-конфигурация, сборка релиза, тесты и синтетический пример CSV.

### Публикация в репозиторий проекта

Скрипт `scripts/publish.sh` принимает адрес целевого репозитория аргументом, копирует только файлы приложения, создаёт коммит и отправляет его обычным `git push`. Локальные видео и результаты в публикацию не входят.

```bash
bash scripts/publish.sh <адрес-репозитория>
```
