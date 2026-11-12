# Установка и обновление у владельца

Сначала [onboarding.md](onboarding.md): существующую память подключают, а не создают заново.
Команды ниже выполняет агент из полного checkout **1.0-preview** (`release/1.0` до публикации).
Аккаунт провайдера, ресурсы, данные и расходы принадлежат пользователю.

## Подготовка

`python` ниже — абсолютный путь Python отдельного окружения с `.[mcp,deployment]`.
`PRIVATE_STATE` и остальные обозначения агент заменяет реальными абсолютными путями/IDs,
цитируя их для текущей оболочки. Для ручного шага дай человеку готовую однострочную команду.
В PowerShell перед заключённым в кавычки путём исполняемого файла нужен `&`;
в Bash/zsh достаточно кавычек. Не переноси shell-синтаксис одной ОС в другую.

Постоянный приватный state находится вне публичного checkout. Здесь Terraform state, SSH-ключ,
журнал HTTPS и секреты. Сохраняй ref/commit в приватной памятке. При повторе используй тот же
state и, после начала миграции, тот же пакет. Не переключай checkout с чужими изменениями.
Для второй памяти используй новый проект Cloud.ru и пустой PRIVATE_STATE. `configure` отказывает
в смене проекта, tenant, бакета или целевого сервера после планирования/установки; смена SSH CIDR
для той же установки остаётся доступной. Перенос сервера — отдельная процедура восстановления,
а не создание новой памяти через старый state. Не исправляй этот отказ ручной подменой IDs.

Клиент: Windows/macOS/Linux, Python 3.11+ и OpenSSH. Сервер управляемого профиля: Ubuntu 24.04,
Python 3.11+, SSH, cron, systemd и постоянный диск. Автоматический HTTPS/Pocket ID — Linux AMD64.
SSH memory installer также проверяется на Linux ARM64. Cloud.ru provider 2.1.6 не имеет
нативного Linux ARM64-бинарника; Windows ARM использует x64-инструменты. `bootstrap` проверяет
платформу и SHA-256 загрузок, не меняет глобальные PATH и terraform.rc.

## Cloud.ru: действия владельца

Давай по одному шагу, пропуская уже выполненные:

1. Войти в [личный кабинет](https://console.cloud.ru), зарегистрироваться при необходимости.
2. Настроить оплату и выбрать свой проект. Проверить возможность использовать Compute/VPC,
   Object Storage, Container Apps, Container Registry и Secret Management.
3. Создать отдельный сервисный аккаунт с правами в выбранном проекте и ключ установки.
   Bootstrap создаёт сеть/ВМ, бакет и его IAM, registry/container/secrets. Не запрашивай
   администратора всей организации. При отказе выясни конкретное недостающее право.
4. Ввести Key ID и Key Secret **локально** через `credentials`, не передавать секрет в чат.

Официальная справка: [ключи и сервисные аккаунты](https://cloud.ru/docs/console_api/ug/topics/guides__api_key).
Персональный ключ наследует права человека — не называй его автоматически ограниченным одним проектом.
Project ID можно сообщить агенту. Tenant ID получает агент чтением; бакет ради него не создаётся.

При включении HTTPS облачный ключ передаётся также на ВМ контроллеру ротации.
**Не отзывай его сразу после установки:** продление сертификатов перестанет работать.
Смена ключа требует согласованной замены в контроллере. Отдельный S3-ключ ограничен бакетом
и служит вложениям/копиям. Обычному OAuth-клиенту эти ключи не нужны.

## Cloud.ru: ВМ и хранилище

```text
python deploy/setup.py --state PRIVATE_STATE init --mode cloudru
python deploy/setup.py --state PRIVATE_STATE doctor
python deploy/setup.py --state PRIVATE_STATE bootstrap
python deploy/setup.py --state PRIVATE_STATE credentials
python deploy/setup.py --state PRIVATE_STATE discover --project-id PROJECT_UUID
python deploy/setup.py --state PRIVATE_STATE catalog --project-id PROJECT_UUID
```

`credentials` запускается в интерактивном терминале владельца; агент готовит окно/команду,
не вводит ключи через свою историю вызовов. Допустимы заранее заданные владельцем
AUTH_KEY_ID/AUTH_SECRET в приватном окружении установщика.

`discover` сохраняет несекретные IDs в discovery.json. Он использует endpoint веб-консоли,
который может меняться. При ошибке проверь права/связь/ответ, затем актуальный способ получения
Tenant ID. Не отправляй человека повторно искать отсутствующий пункт «Параметры работы с API».

Из catalog.json выбери доступный Ubuntu 24.04 AMD64, зону и конфигурацию без GPU с достаточным
диском/RAM. Не копируй UUID из чужого пилота. Создай несекретный OPTIONS_JSON:

```json
{
  "project_id": "PROJECT_UUID",
  "tenant_id": "TENANT_UUID",
  "zone": "ZONE_FROM_CATALOG",
  "image_id": "UBUNTU_AMD64_IMAGE_UUID",
  "flavor": "FLAVOR_FROM_CATALOG",
  "ssh_cidr": "CURRENT_PUBLIC_IPV4/32",
  "disk_gb": 40
}
```

SSH CIDR — проверенный текущий выход установщика к ВМ. Получение IP у стороннего сервиса
согласуй, если среда этого требует. Не открывай SSH всему интернету. При VPN/смене сети используй
[network.md](network.md) на Windows/macOS/Linux, не старые IP и номера интерфейсов из переписки.
Обычный HTTPS-доступ не требует повторного изменения SSH-маршрутов; обслуживание может требовать.

Бакет по умолчанию STANDARD; SINGLE — одна зона, объясни отличие владельцу до выбора.
Проверяй уведомления провайдера. DNS задан шаблоном, при необходимости задаётся `dns_servers`.
quota=0 отключает лимит размера бакета и не является лимитом расходов.

```text
python deploy/setup.py --state PRIVATE_STATE configure --file OPTIONS_JSON
python deploy/setup.py --state PRIVATE_STATE plan
```

Покажи конкретный план: ВМ, SSD, IPv4, сеть, закрытый версионируемый бакет, отдельный S3-ключ.
Для полного HTTPS-пути сразу учти также registry, контейнер, Secret Management и трафик.
Оценку возьми из актуального калькулятора/тарифов под выбранную конфигурацию; установщик её
не вычисляет. Если оценка недоступна, объясни неопределённость и уточни допустимый бюджет.
После согласования плана либо в пределах уже данного разрешения:

```text
python deploy/setup.py --state PRIVATE_STATE apply --approve-plan PLAN_SHA256
python deploy/setup.py --state PRIVATE_STATE https-plan
```

Первый hash берётся из plan-review.json. `https-plan` сохраняет отдельный план mTLS ingress
и намерение создания HTTPS-ресурсов в https-plan.json. Здесь другой hash: согласуй добавления,
если они не были разрешены ранее. `apply` ещё не означает работающий MCP.
Удаление/замена ресурсов отклоняется; после сбоя не удаляй state и не создавай вторую ВМ.

## Cloud.ru: HTTPS, вход и резервирование

```text
python deploy/setup.py --state PRIVATE_STATE install --https --approve-plan HTTPS_PLAN_SHA256
python deploy/setup.py --state PRIVATE_STATE https-check
```

Команда сама устанавливает память на ВМ, проверяет restic restore, создаёт приватный registry
с фиксированным образом и контейнер, получает адрес `*.containerapps.ru`, настраивает OAuth/MCP,
проверяет mTLS перед открытием ingress, публикует вход и настраивает продление сертификатов
и отдельные копии OAuth. Покупка домена не нужна. Min/max контейнера 0/1; платный постоянно
работающий экземпляр не включается. Краткие разрывы при публикации возможны.

SQLite, оригиналы, вики и OAuth-данные живут на постоянном диске ВМ. Контейнер — только вход.
Службы systemd переживают завершение SSH и перезапуск; расписания restic работают отдельно.

| Результат в PRIVATE_STATE | Назначение |
|---|---|
| installed.json | Серверный root, пакет и ID памяти |
| https-installed.json, client-https.json | Endpoint, OAuth и проверки; это не универсальный конфиг любого приложения |
| client-config.json | SSH/stdio для резервного доступа и CLI массовой отправки |
| owner-enrollment.json | Receipt с путём к отдельному приватному файлу кода; код не передавать в чат |
| recovery-kit-receipt.json | Путь к комплекту восстановления и проверка |
| rotation-installed.json | Расписание продления сертификатов |

Владелец открывает локальный файл из выдачи owner-login, сам вводит код по login_url и создаёт
passkey. Агенту нужен результат, не код. Истёкший код обновляется командой `owner-login`,
без пересоздания аккаунта. Затем отдельный вход приложения по [connection.md](connection.md).
Перед подключением Claude Code к старой установке выполни
`python deploy/setup.py --state PRIVATE_STATE oauth-client --client claude-code`:
это добавляет его точный localhost callback с резервной копией OAuth, сохраняя вход Codex.

Комплект восстановления содержит секреты: попроси сохранить защищённую копию вне компьютера
и ВМ, в памятке оставь только путь. Актуальный комплект:
`python deploy/setup.py --state PRIVATE_STATE recovery-kit`.
Комплект не заменяет доступ к облачному аккаунту для восстановления ВМ.

## Свой сервер: рабочая альтернатива

Проверь ОС, Python, SSH-ключ, место, cron и доступность из нужных сетей. За домашним NAT используй
уже работающую сеть/VPN; роутер установщик не перенастраивает. На Ubuntu при необходимости
установи python3, python3-venv, python3-pip, openssh-server и cron, включи ssh/cron.
Используй отдельного пользователя с входом по ключу.

```text
python deploy/setup.py --state PRIVATE_STATE init --mode server
python deploy/setup.py --state PRIVATE_STATE configure --file SERVER_OPTIONS_JSON
python deploy/setup.py --state PRIVATE_STATE install
python deploy/setup.py --state PRIVATE_STATE check
```

SERVER_OPTIONS_JSON: `host`, `ssh_user`, `ssh_port` (обычно 22), абсолютный `identity_file`,
`schedule_backup: true`. В Windows JSON используй `/` или экранированные обратные слеши.
Подключи client-config.json по connection.md. `check` использует локальный MCP SDK и сверяет
workspace ID. Изменившийся SSH host key требует проверки сервера, не отключения защиты.
Windows установщик предпочитает Git OpenSSH; другой бинарник задаётся `ssh_command` и
проверяется реальным MCP, не только успешным вызовом ssh.

По умолчанию restic локальный: потеря диска затронет и копии. Для своего S3 владелец локально
заполняет PRIVATE_STATE/backup-config.json: endpoint (HTTPS), region, bucket, access_key, secret_key.
Установщик проверяет копию/восстановление; учти страну и права хранилища. Terraform/Cloud.ru
для SSH-пути не нужны. Универсальный HTTPS bootstrap для любого reverse proxy пока не реализован:
`install --https` использует Cloud.ru. Ручной HTTPS-путь — `docs/oauth-preview.md` и
`docs/oauth-recovery.md` в том же checkout.

## Обновление и ошибки

Проверь checkout, release notes, текущие версии, workspace ID, состояние копий и новый ref/commit.
Не теряй пользовательские изменения. Обнови checkout/локальное окружение и выполни:
`python deploy/setup.py --state PRIVATE_STATE update`.
Сохраняются память, владелец, ключи, endpoint и ротация. Новый plan для смены только пакета не нужен;
изменения облачных параметров требуют отдельного просмотра.

Переподключи MCP, проверь реальный memory_status и чтение сохранённого материала. https-check —
дополнительная проверка владельца через SSH. Обнови всю копию скилла и capture-клиент из той же
версии; client_setup.py не обновляет сервер. Сохрани личные правки и заметки в START-HERE.md.

При прерванной миграции повтори **тот же пакет и state**. Maintenance gate закрывает запись:
не удаляй его вручную и не запускай старый код поверх новой БД. Сначала закончи этот шаг либо
восстанавливай соответствующую копию в новую папку.

Если CREATE потерял ответ, сверь ID с сохранённым намерением:
`python deploy/setup.py --state PRIVATE_STATE https-reconcile --stage registry --resource-id REVIEWED_ID`
(или `--stage container`), затем повтори установку. Не меняй имя/проект/state для обхода ошибки.
Повторяющаяся одинаковая ошибка требует диагностики. Покажи команду, этап журнала и безопасный
код ошибки; сырые логи/state/секреты не публикуй. При cloud-init ошибке проверь status --long
и приватный серверный журнал, удаляя секреты из любого цитируемого фрагмента.

Потеря passkey при доступной ВМ — `owner-login`; потерянные устройства отзываются отдельно.
Потеря ВМ/диска — `docs/oauth-recovery.md`: новая папка, проверка памяти, свежая OAuth-идентичность
и явное переключение. Старые passkey/сессии не восстанавливаются.
Штатные копии — зашифрованный restic с оригиналами; ZIP нужен только для старых архивов.
Очистка и retention — [recovery-and-folders.md](recovery-and-folders.md).

Удаление клиента не прекращает облачные начисления. Закрытие установки — отдельная операция:
внешняя проверенная копия, остановка клиентов/расписаний и осознанное удаление ресурсов.
Универсального destroy нет; prevent_destroy защищает диск/ВМ/бакет от случайного удаления.
