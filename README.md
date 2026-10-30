<div align="center">

<img src="assets/mandela.jpg" width="180" alt="Mandela">

# Mandela

**Синтаксис Python · возможности C++ · строгость Rust**

Компилятор Mandela написан на самой Mandela.

[English](README_EN.md) | Русский

[![License: GPL v3](https://img.shields.io/badge/license-GPL--3.0-blue.svg)](LICENSE)
[![Stage](https://img.shields.io/badge/stage-v0.1%20bootstrap-orange.svg)]()
[![Tests](https://img.shields.io/badge/tests-17%20passing-brightgreen.svg)](tests/run_tests.sh)

</div>

---

Mandela — язык программирования общего назначения, нацеленный и на системный низкоуровневый код, и на повседневные задачи. Синтаксис максимально приближен к Python: блоки по отступам, понятные команды, минимум церемоний. Под капотом — дисциплина Rust: статическая типизация, неизменяемость по умолчанию и компилятор, который не пропустит ошибку. В перспективе — производительность и контроль уровня C++.

## Философия

| Из языка | Что берём |
| --- | --- |
| **Python** | Синтаксис: отступы вместо скобок, `def`, `for x in ...`, `print`, `len` |
| **Rust** | Строгость: статическая типизация, `let`/`let mut`, ошибки в формате rustc, паники с кодом 101 |
| **C++** | Прикладь к железу: целочисленная семантика C, путь к ручной памяти и `unsafe` |

## Быстрый старт

```bash
git clone https://github.com/mel0k1/mandela.git
cd mandela
./bin/mandela run examples/hello.mnd
```

Ничего собирать не нужно: нужен только `python3` (бутстрап-движок) и всё.

## Пример

```python
# examples/fib.mnd
def fib(n: int) -> int:
    if n < 2:
        return n
    return fib(n - 1) + fib(n - 2)

for i in range(0, 11):
    print("fib(" + str(i) + ") = " + str(fib(i)))
```

```text
$ ./bin/mandela run examples/fib.mnd
fib(0) = 0
fib(1) = 1
...
fib(10) = 55
```

Даже ASCII-мандельброт пишется на целых числах — [examples/mandel.mnd](examples/mandel.mnd).

## Компилятор не пропустит

`./bin/mandela check` ловит ошибки до запуска и оформляет их как rustc:

```text
error[E0384]: cannot assign to `n`, as it is not declared as mutable
  --> demo.mnd:5:1
  |
5 | n = "ten"
  | ^
error[E0308]: mismatched types: expected `int`, found `str`
  --> demo.mnd:5:1
  |
5 | n = "ten"
  | ^
error[E0061]: this function takes 2 arguments but 1 arguments were supplied
  --> demo.mnd:6:11
  |
6 | print(area(2))
  |           ^
```

Коды ошибок совпадают с rustc там, где смысл совпадает: `E0308`, `E0369`, `E0384`, `E0061`, `E0425`, `E0428`, `E0277`, `E0268`, `E0267`, `E0572`, `E0282`, `E0412`, `E0594`, `E0618`. Полный список — в [спецификации](spec/SPEC.md).

## Архитектура: компилятор на самом себе

Главная особенность Mandela — **самохостинг**. Фронтенд компилятора (лексер, парсер, тайп-чекер) написан не на Python и не на C, а на самой Mandela:

```text
          ваш код .mnd
               │
               ▼
   ┌───────────────────────────┐
   │  src/*.mnd                │  ← компилятор Mandela,
   │  lexer · parser · checker │    написанный на Mandela
   └────────────┬──────────────┘
                │  ошибки в стиле rustc
                ▼
        проверка пройдена?
                │
                ▼
   ┌───────────────────────────┐
   │  bootstrap/mandela.py     │  ← бутстрап-движок (Python),
   │  временный исполнитель    │    исполняет и компилятор, и код
   └───────────────────────────┘
```

`bootstrap/mandela.py` — это временные «строительные леса»: минимальный интерпретатор, существующий только чтобы запустить самохостящийся компилятор. Он будет убран, когда Mandela начнёт генерировать машинный код (см. roadmap).

## CLI

```bash
./bin/mandela run file.mnd [args...]   # проверить и выполнить
./bin/mandela check file.mnd           # только строгая проверка
./bin/mandela lex file.mnd             # дамп токенов (отладка)
./bin/mandela ast file.mnd             # дамп AST в s-выражениях (отладка)
```

## Возможности v0.1

- Статическая типизация с выводом типов: `let x = 5`, `let name: str = "..."`, `list[int]`
- Неизменяемость по умолчанию: `let` против `let mut`
- Целые числа (семантика деления C), строки, `bool`, `nil`, списки
- `if/elif/else`, `while`, `for x in range(...)`, `for x in список`, `break`, `continue`
- Функции с обязательными аннотациями параметров, рекурсия, хоистинг функций
- Встроенные: `print len str int chr ord push pop split substr read_file write_file args fatal exit range`
- Строгий тайп-чекер с диагностиками rustc, предупреждения о неиспользуемых переменных

## Roadmap

| Версия | Что планируется |
| --- | --- |
| v0.2 | Кодогенерация в C и нативные бинарники, уход от бутстрап-движка |
| v0.3 | `unsafe`-блоки, `ptr<T>`, ручное управление памятью |
| v0.4 | Числа с плавающей точкой, структуры, методы |
| v0.5 | Модули вместо линейных скриптов, менеджер пакетов |

## Разработка

```bash
make test        # 17 тестов: позитивные и негативные
```

Структура репозитория:

```text
bin/mandela          CLI
bootstrap/mandela.py бутстрап-движок (временно)
src/*.mnd            компилятор, написанный на Mandela
spec/SPEC.md         спецификация языка v0.1
examples/            примеры программ
tests/               тесты (позитивные и негативные)
```

## Лицензия

[GPL-3.0](LICENSE) © mel0k1
