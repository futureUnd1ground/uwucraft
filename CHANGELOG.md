# Changelog

## 0.4.0 — 2026-11-01

- Несколько независимых серверов: CLI --profile, profile list/use, TUI переключатель, отдельные миры/Java/порты/units/tasks и безопасный default legacy профиль.
- Официальный каталог версий CLI/TUI, автоматический незанятый порт, Java 8 fallback для старых Mojang manifests, legacy Forge launcher и общая версия NeoForge prefix mapping.
- Совместимость Java-клиентов: ViaVersion/ViaBackwards, опциональный ViaRewind, backup и управляемый restart, SHA-256 и staged plugin install.
- Windows ZIP содержит официальный подписанный установщик Python; установка без winget, с проверкой SHA-256 и PSF Authenticode. Git checkout скачивает тот же файл автоматически.
- Backup/restore default сохраняет другие профили; старые фоновые units фиксируются на default без перезапуска мира.

## 0.3.0 — 2026-11-01

- Нативный Windows backend: APPDATA/LOCALAPPDATA, Windows file locks и защищённая локальная named-pipe консоль.
- Автоустановка Temurin ZIP с java.exe и Forge/NeoForge win_args.txt; единый мастер CLI/TUI сохранён.
- Defender Firewall через одноразовое UAC, маршруты через PowerShell, буфер Windows, UPnP/NAT-PMP без изменения общего протокольного backend.
- Пользовательский Task Scheduler после входа, watchdog crash/restart, явный stop, ежедневные backups и отдельное продление проброса.
- install.cmd/install.ps1, ярлык и пользовательский PATH; Windows CI с настоящим JVM lifecycle и Java download.
- UTF-8 для конфигураций и журналов на обеих ОС; Linux backend и существующий мир сохранены.

## 0.2.0 — 2026-11-01

- Общий backend создания сервера: CLI `create`, короткие алиасы `auto`/`install` и TUI «Создать сервер».
- Семь необходимых параметров, автоматические defaults и явная EULA; после неё конфигурационных вопросов нет.
- Прямой публичный IPv4, узкое исключение nftables, UPnP/NAT-PMP с продлением аренды, независимый внешний probe и проверка MOTD новой установки.
- Отчёт о правах, CGNAT, автопробросе и доступности без ложного «успеха»; итоговая карточка и копирование только публичного адреса.
- Удалён прежний дополнительный сетевой backend; миграция отключает его сервис, сохраняя сервер и мир.
- Расширены CLI/TUI/protocol tests и добавлена реальная nftables-проверка в изолированном network namespace.

## 0.1.1 — 2026-11-01

Исправлена согласованность CLI/TUI статуса внешней проверки.

## 0.1.0 — 2026-11-01

Первый рабочий релиз CLI/TUI, пользовательского systemd-сервиса,
приватной консоли, официальных установщиков, backup/restore, контента и тем.
