"""Read-only explanation of one installation, from its private local receipts."""
from setup import read

STAGES = {
    'infrastructure': 'ВМ, публичный IPv4 и приватный бакет',
    'project': 'Проект Timeweb', 'ssh_key': 'Регистрация открытого SSH-ключа',
    'server': 'Выделение ВМ выбранного тарифа', 'ipv4': 'Назначение IPv4 этой ВМ',
    'bucket': 'Создание и проверка S3', 'memory': 'Проверка или установка памяти на ВМ',
    'ssh': 'Первый ответ ВМ по SSH', 'cloud-init': 'Завершение cloud-init на ВМ',
    'dependencies': 'Подготовка Python и системных служб на ВМ',
    'memory-install': 'Установка памяти и проверка резервной копии',
    'architecture': 'Проверка архитектуры сервера', 'edge': 'Подготовка HTTPS-приложения',
    'git': 'Подключение репозитория', 'app': 'Выделение HTTPS-приложения',
    'edge-retry': 'Повтор сборки существующего приложения', 'edge-deploy': 'Запуск приложения',
    'dns': 'Последовательные проверки DNS и HTTPS', 'server-check': 'Проверка установленного входа',
    'oauth': 'Настройка OAuth на ВМ', 'publish': 'Публикация входа с взаимным TLS',
    'renewal': 'Настройка продления сертификатов', 'public-check': 'Проверка публичного HTTPS и OAuth',
    'recovery': 'Проверка комплекта восстановления', 'owner': 'Подготовка первого входа владельца',
}


def optional(state, filename):
    path = state / filename
    return read(path) if path.is_file() else {}


def describe(state, journal, status):
    plan = optional(state, 'timeweb-plan.json')
    config = optional(state, 'deployment.json')
    cleanup = optional(state, 'timeweb-cleanup.json')
    resources = status['resources']
    rejected = status['app_capacity_rejected']
    pending = [k for k, v in resources.items() if v['creation_unresolved']]
    removed = {k for k, step in cleanup.get('steps', {}).items() if step.get('absent')}
    recorded = [k for k, v in resources.items() if v['id'] is not None and k not in removed]
    confirmed = list(recorded)
    ipv4 = journal.get('server_ipv4', {})
    if (config.get('host') or ipv4.get('ip') or journal.get('ipv4_verified')) and 'ipv4' not in removed:
        confirmed.append('ipv4')
    unidentified_ip = ipv4.get('submitted') and 'ipv4' not in confirmed and 'ipv4' not in removed
    if unidentified_ip:
        pending.append('ipv4')
    prices = {}
    for resource, tariff, field in [('server','servers','server_preset_id'), ('bucket','storage','storage_preset_id'),
                                    ('app','apps','app_preset_id')]:
        if resource in recorded:
            row = plan.get('presets', {}).get(tariff, {})
            if resource == 'app' and status['app_selection']:
                row = status['app_selection']
            elif row.get('id') != config.get(field):
                row = {}
            prices[resource] = row.get('price')
    if 'ipv4' in confirmed:
        prices['ipv4'] = None  # negotiated/variable charges are outside the preset catalog
    known = [v for v in prices.values() if isinstance(v,(int,float))]
    cost = {'monthly_rub_by_resource': prices, 'known_monthly_subtotal_rub': round(sum(known),2),
            'approx_hourly_subtotal_rub': round(sum(known)/730,3),
            'unknown_creation': pending,
            'basis': 'Оценка по сохранённым тарифам и ID. null — цена неизвестна. IPv4, трафик и операции уточнить по счёту. Автоплатёж проверяется отдельно.'}

    stage = journal.get('step') or status['stage']
    action = 'install --https'
    waiting = STAGES.get(stage, 'Продолжение установки')
    public = optional(state, 'https-installed.json')
    public_ok = all(public.get('checks', {}).get(k) is True for k in ('transport','oauth_metadata','unauthenticated_rejected'))
    auto = status.get('automatic_selection') or {}
    if not cleanup:
        if (state/'installed.json').is_file() and 'server' in recorded:
            confirmed.append('memory_install_receipt')
        if public_ok:
            confirmed.append('https_oauth_checks_passed_at_install')
    if cleanup:
        action = 'timeweb-cleanup'
        waiting = 'Проверка остатка ресурсов по плану очистки'
        if cleanup.get('all_planned_resources_absent'):
            waiting, action = 'Удаление завершено', 'Сверить итоговый счёт Timeweb'
    elif (state/'timeweb-update.json').exists() and not optional(state,'timeweb-update.json').get('complete'):
        waiting, action = 'Незавершённое обновление', 'Продолжить timeweb-update с ранее согласованным коммитом'
    elif (state/'timeweb-vm-selection.json').exists() and not optional(state,'timeweb-vm-selection.json').get('complete'):
        waiting, action = 'Запись согласованного тарифа ВМ', 'Продолжить timeweb-select-vm с теми же параметрами и хэшем'
    elif pending:
        waiting = 'Сверка результата отправленного запроса: ' + ', '.join(pending)
        action = 'timeweb-diagnostics'
    elif auto.get('status') == 'running':
        waiting = 'Выделение ВМ из согласованной очереди'
        action = 'Продолжить timeweb-auto-run с тем же хэшем и прежними условиями оплаты'
    elif rejected:
        waiting = 'Выбор после отказа App Platform'
        action = 'timeweb-select-app (цены) или timeweb-cleanup (просмотр плана)'
    elif auto.get('status') in ('stopped','exhausted') and ('server' not in recorded or status['stage']=='infrastructure'):
        waiting, action = 'Согласованный поиск ВМ остановлен', 'timeweb-diagnostics'
    elif status['vm_rejection']:
        waiting, action = 'Выбор после отказа в выделении ВМ', 'timeweb-vm-options'
    elif status['app_selection'] and 'app' not in resources:
        waiting, action = 'Заказ согласованного тарифа приложения', 'install --https'
    elif status.get('last_error') and status['last_error'].get('http_status'):
        action = 'timeweb-diagnostics; после устранения ошибки продолжить текущую стадию'
    elif (status['stage']=='complete' or (not journal.get('last_error') and stage=='owner' and (state/'owner-enrollment.json').is_file())) and public_ok:
        waiting = 'Вход владельца в системном браузере, OAuth и чтение из выбранного клиента'
        action = 'Использовать owner-enrollment.json; затем вызвать memory_status из Cursor и сверить workspace_id'
    elif not plan and not recorded:
        if not config:
            waiting, action = 'Выбор папки установки', 'init --mode timeweb'
        elif not (state/'credentials.json').exists():
            waiting, action = 'Ввод токена в локальном терминале', 'credentials'
        elif not config.get('server_preset_id'):
            waiting, action = 'Выбор параметров и тарифов', 'catalog, затем configure'
        else:
            waiting, action = 'Проверка плана расходов', 'plan'
    elif (state/'timeweb-auto-plan.json').exists() and not auto and 'server' not in recorded:
        waiting, action = 'Согласование очереди и стоимости', 'Проверить timeweb-auto-plan и автоплатёж перед timeweb-auto-run'
    elif status['stage'] in ('not_started','infrastructure') and not config.get('host'):
        waiting = STAGES.get(stage,'Согласование заказа ВМ и инфраструктуры')
        action = 'После согласования: apply --approve-plan '+str(plan.get('plan_sha256','<хэш плана>'))
    elif stage == 'infrastructure' and config.get('host') and 'bucket' in recorded and not status.get('last_error'):
        waiting = 'Доступ по SSH и установка памяти'
    verified = {} if cleanup else journal.get('verified_stages', {})
    if not cleanup and stage in ('cloud-init','dependencies','memory-install'):
        confirmed.append('ssh_responded_this_install')
    return {'confirmed_from_local_state':confirmed, 'last_success_at':verified, 'waiting_for':waiting, 'cost':cost,
            'resume':action, 'same_state':str(state)}
