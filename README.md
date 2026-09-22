# EVM Inventory and Bitget Consolidation

CLI для инвентаризации EVM-кошельков, подготовки маршрутов к депозиту на Bitget и их явного выполнения. По умолчанию команды только читают данные и создают файлы; подпись и отправка транзакций возможны исключительно с флагом `--execute`.

> Внимание: `execute-routes --execute` может перемещать средства. Сначала соберите инвентарь и сформируйте план, затем проверьте его JSON и только после этого запускайте выполнение.

## Установка и доступы

```bash
uv sync
cp .env.example .env
```

В `.env` указываются только нужные конкретному режиму ключи:

| Переменная | Для чего нужна |
| --- | --- |
| `ALCHEMY_API_KEY` | Более полное обнаружение ERC-20 при сканировании (необязательно). |
| `ALCHEMY_RPC_API_KEY` или `ALCHEMY_API_KEY` | RPC для отправки транзакций при выполнении. |
| `BITGET_API_KEY`, `BITGET_SECRET_KEY`, `BITGET_PASSPHRASE` | Получение живого каталога депозитов и выполнение маршрутов. |
| `MAX_ROUTE_LOSS_PCT` | Максимальная суммарная потеря на кошелёк и исходную сеть; по умолчанию `15`, допустимый диапазон `(0, 100]`. |

Приватные ключи берутся из workbook только на время выполнения и не записываются в отчёты, план или журнал.

## Режимы работы

### 1. Проверить входной список без сети

`scan --dry-run` валидирует файл с адресами и не делает RPC/API-запросов:

```bash
uv run evm-inventory scan \
  --wallets input/wallets.txt \
  --db out/inventory.sqlite \
  --dry-run
```

Файл содержит по одному публичному `0x`-адресу на строку.

### 2. Собрать инвентарь кошельков

Обычный `scan` читает балансы нативных монет, известных токенов и результаты discovery. Транзакции не подписываются и не отправляются.

```bash
uv run evm-inventory scan \
  --wallets input/wallets.txt \
  --db out/inventory.sqlite
```

Инвентарь сохраняется в SQLite. Перед планированием убедитесь, что нужные сети и токены имеют полное покрытие: отсутствие цены, RPC-ответа или discovery не считается нулевым балансом.

Для выгрузки уже собранного JSON в плоский CSV:

```bash
uv run evm-inventory export \
  --db out/inventory.sqlite \
  --output out/inventory-export
```

Экспорт создаёт `balances.csv`, `checks.csv`, `coverage.csv` и `inventory.json` в `out/inventory-export`.

### 3. Подготовить и проверить workbook

Шаблон содержит адрес кошелька, приватный ключ и адрес депозита Bitget:

```bash
uv run evm-inventory workbook-template \
  --output input/wallets.xlsx
```

Проверка workbook не подписывает транзакции, но требует корректные поля у всех его строк и может записать служебные статусы в файл:

```bash
uv run evm-inventory workbook-dry-run \
  --workbook input/wallets.xlsx
```

### 4. Сделать план без отправки средств

Основной режим планирования получает живой каталог депозитов Bitget и котировки LI.FI, но остаётся read-only: кошельки не разблокируются и транзакции не создаются.

```bash
uv run evm-inventory quote-routes \
  --balances out/inventory-export/balances.csv \
  --workbook input/wallets.xlsx \
  --output out/routes-plan.json \
  --wallet-ranges 5 \
  --quote-floor 0.01
```

`--wallet-ranges 5` выбирает только кошелёк №5; допустимы отдельные номера и диапазоны, например `1,3-5,8`. `--quote-floor` — минимальная оценка позиции в USD для запроса котировки.

Также доступен упрощённый офлайн-классификатор без Bitget и LI.FI. Он не даёт исполнимых котировок — для реального маршрута используйте `quote-routes`:

```bash
uv run evm-inventory route-plan \
  --balances out/inventory-export/balances.csv \
  --output out/offline-plan.json \
  --quote-floor-raw 0.01
```

#### Правила планировщика

- Депозит допускается только при точном совпадении `chain_id` и идентификатора актива с живой сетью и монетой Bitget.
- USD-стоимость используется только при полной свежей оценке. Неполные, отсутствующие или некорректные цены переводят позицию в ручную проверку.
- Планировщик рассматривает живые котировки LI.FI для включённых целей Bitget и проверяет точные идентификаторы исходного и целевого активов, сумму, получателя и структуру исполняемых шагов.
- Для каждого кошелька и исходной сети планировщик выбирает маршрут с наименьшей оценённой потерей; сумма потерь и оплачиваемого кошельком газа ограничена `MAX_ROUTE_LOSS_PCT` от стоимости исполнимых балансов этой группы.
- Балансы с действиями `deny` или `review`, пылевые суммы и строки без адреса депозита не увеличивают знаменатель группы и не допускаются к отправке.
- Допустимое направление и провайдер заранее не задаются в конфигурации: LI.FI-котировка принимается только после структурной проверки маршрута и точного совпадения идентичности активов.
- Если свежая проверка маршрута, цены, газа, целевого минимума Bitget или бюджета группы не проходит, позиция получает `manual_review`, а оставшиеся отправки этой группы останавливаются.
- Для последовательности `swap_to_native → bridge` исполнитель измеряет фактический результат swap, повторно котирует bridge с фактического native-баланса и сохраняет старый и новый маршрут в journal. Неоднозначный результат отправки не приводит к слепому повтору транзакции.

Список `assets` в `config/swap-allowlist.json` по-прежнему задаёт действия для точных пар `chain_id` и `asset_id`; одного символа для допуска актива недостаточно.

### 5. Выполнить утверждённый план

Сначала запустите команду без `--execute`: она проверит план, workbook и каталог без подписи:

```bash
uv run evm-inventory execute-routes \
  --plan out/routes-plan.json \
  --workbook input/wallets.xlsx \
  --catalog src/evm_inventory/data/catalog.json \
  --journal out/execution-journal.sqlite
```

Только после проверки добавьте `--execute`:

```bash
uv run evm-inventory execute-routes \
  --plan out/routes-plan.json \
  --workbook input/wallets.xlsx \
  --catalog src/evm_inventory/data/catalog.json \
  --journal out/execution-journal.sqlite \
  --wallet-ranges 5 \
  --execute
```

Исполнитель хранит намерение, nonce и хеш транзакции в journal до и после broadcast. При неоднозначном RPC-ответе он восстанавливает состояние по nonce/хешу вместо слепой повторной отправки. Причина ошибки сохраняется в journal рядом со статусом маршрута.

### 6. Посмотреть состояние выполнения и восстановить контекст

```bash
uv run evm-inventory resume-routes \
  --journal out/execution-journal.json
```

Команда только читает journal и выводит счётчики групп, позиций и транзакционных шагов, а также причины ручной проверки. Она не загружает workbook или ключи, не делает RPC-запросов, не меняет journal и не отправляет повторные транзакции. Это первый шаг после прерывания или ошибки выполнения; состояния `source_asset_converted_bridge_pending`, `requote_required` и `manual_review_after_swap` требуют ручного разбора.

### Сквозной запуск

```bash
uv run evm-inventory scan --wallets input/wallets.txt --db out/inventory.sqlite
uv run evm-inventory export --db out/inventory.sqlite --output out/inventory-export
uv run evm-inventory quote-routes --balances out/inventory-export/balances.csv \
  --workbook input/wallets.xlsx --output out/routes-plan.json --wallet-ranges 5
```

Проверьте JSON-план: конкретную сеть и актив Bitget, адрес получателя, сумму, шаги и групповой `loss_pct`. Затем выполните выбранные диапазоны только с явным `--execute` и после завершения или прерывания прочитайте journal:

```bash
uv run evm-inventory execute-routes --plan out/routes-plan.json \
  --workbook input/wallets.xlsx --catalog src/evm_inventory/data/catalog.json \
  --journal out/execution-journal.sqlite --wallet-ranges 5 --execute
uv run evm-inventory resume-routes --journal out/execution-journal.sqlite
```

## Что проверять перед боевым прогоном

1. Инвентарь покрывает все нужные сети и токены, а проблемные строки разобраны.
2. В `routes-plan.json` нет `manual_review`, `unknown` или неподтверждённых действий.
3. Сумма, комиссия, газовый резерв, сеть назначения и адрес депозита экономически приемлемы для каждого маршрута.
4. Для каждого маршрута проверьте точную цель Bitget, получателя, идентичности активов и структурные данные котировки LI.FI.
5. Journal сохранён в устойчивом месте и перед повторным запуском проверен через `resume-routes`.

## Безопасность

Не добавляйте `.env`, workbook с приватными ключами, планы с чувствительными данными и execution journal в Git. Используйте отдельный тестовый кошелёк перед первым боевым запуском и ограничивайте `--wallet-ranges`, пока не убедитесь в поведении на одном кошельке.
