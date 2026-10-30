<div align="center">

<img src="assets/mandela.jpg" width="180" alt="Mandela">

# Mandela

**Простой синтаксис · Строгая типизация · Нативный код**

Компилятор Mandela написан на самой Mandela.

[English](README_EN.md) | Русский

[![License: GPL v3](https://img.shields.io/badge/license-GPL--3.0-blue.svg)](LICENSE)
[![Stage](https://img.shields.io/badge/stage-v0.3%20native%20codegen-orange.svg)]()
[![Tests](https://img.shields.io/badge/tests-50%20passing-brightgreen.svg)](tests/run_tests.sh)

</div>

---

Mandela — язык программирования общего назначения. Лаконичный синтаксис на
отступах: блоки без скобок и точек с запятой, программы читаются как псевдокод.
Строгая статическая типизация с выводом типов, неизменяемость по умолчанию.
Подробные диагностики компилятора ловят ошибки до запуска. А `mandela build`
собирает программу в нативный исполняемый файл — машинный код для вашей
платформы.

## Принципы

- **Упрощённый синтаксис.** Блоки по отступам, `def`, `for x in ...`,
  `print`, `len` — минимум церемоний, максимум читаемости.
- **Строгая типизация.** Тип каждой переменной известен до запуска. Неявных
  приведений нет; `let` неизменяем по умолчанию, мутация требует `let mut`.
- **Честные ошибки.** Каждая диагностика — с номером (`error[E0308]`), файлом,
  строкой и кареткой под проблемным токеном. Все ошибки за один проход.
- **Нативный результат.** Один файл `.mnd` → проверка → промежуточное
  представление → машинный код системным тулчейном.

## Быстрый старт

```bash
git clone https://github.com/mel0k1/mandela.git
cd mandela
./bin/mandela run examples/hello.mnd      # мгновенный старт: интерпретатор
./bin/mandela build examples/hello.mnd    # или сразу нативный бинарник
./hello
```

Нужен только `python3` (бутстрап-движок). Для `build` дополнительно — любой
системный компилятор (`cc`, `gcc`, `clang`).

## Пример

```mandela
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

## Структуры и методы

```mandela
# examples/structs.mnd
struct Point:
    x: int
    y: int

    def dot(self, other: Point) -> int:
        return self.x * other.x + self.y * other.y

    def move_by(mut self, dx: int, dy: int):
        self.x = self.x + dx
        self.y = self.y + dy

def main():
    let mut a = Point { x: 1, y: 2 }
    let b = Point { x: 3, y: 4 }
    a.move_by(10, 20)      # ок: a объявлен let mut
    print(a.dot(b))
```

Метод, изменяющий `self`, объявляется `mut self`; вызов такого метода на
неизменяемой переменной — ошибка компиляции (`E0596`).

## Словари и строковые методы

```mandela
# examples/dicts.mnd
let mut freq: dict[str, int] = {}
for w in "to be or not to be".split(" "):
    if freq.has(w):
        freq[w] = freq[w] + 1
    else:
        freq[w] = 1
print(freq)                       # {"to": 2, "be": 2, "or": 1, "not": 1}

print("  Hello  ".trim().upper()) # HELLO
print("mandela".replace("a", "4"))# m4ndel4
```

Словари упорядочены по вставке, ключи — `str` или `int`. Методы словарей:
`keys`, `values`, `has`, `remove`. Строковые методы: `upper`, `lower`, `trim`,
`split`, `contains`, `starts_with`, `ends_with`, `replace`, `find`, `substr`,
`repeat`, `chars`.

## Компилятор не пропустит

`./bin/mandela check` ловит ошибки до запуска и показывает каждую с номером,
местом и кареткой:

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

Диагностики нумеруются кодами: `E0061`, `E0062`, `E0063`, `E0308`, `E0369`,
`E0384`, `E0412`, `E0425`, `E0428`, `E0596`, `E0599`, `E0609` и другими.
Полный список — в [спецификации](spec/SPEC.md).

## Сборка в нативный код

```bash
./bin/mandela build program.mnd -o program   # проверка → нативный бинарник
./program
./bin/mandela build program.mnd --emit c     # дамп промежуточного представления
./bin/mandela build program.mnd --emit asm   # ассемблерный листинг
```

Конвейер: самохостящийся чекер проверяет типы → бэкенд понижает AST до
самодостаточного промежуточного представления — одного переносимого исходника,
в котором рантайм описан горсткой структур: тегированное значение, строка,
список, структура, словарь; структуры пользователя ложатся в таблицы
определений, методы — в обычные функции. Дальше системный компилятор собирает
машинный код с `-O2`. Тесты гарантируют, что интерпретатор и нативный бинарь
выводят одно и то же.

## Архитектура: компилятор на самом себе

Главная особенность Mandela — **самохостинг**. Фронтенд компилятора (лексер,
парсер, тайп-чекер) написан на самой Mandela:

```text
          ваш код .mnd
               │
               ▼
   ┌───────────────────────────┐
   │  src/*.mnd                │  ← компилятор Mandela,
   │  lexer · parser · checker │    написанный на Mandela
   └────────────┬──────────────┘
                │  error[E0308] с кареткой
                ▼
        проверка пройдена?
                │
      ┌─────────┴──────────┐
      ▼                    ▼
┌────────────────┐   ┌────────────────────────┐
│ bootstrap/     │   │ bootstrap/codegen.py   │
│ mandela.py     │   │ AST → IR → cc -O2      │
│ интерпретатор  │   │ нативный бинарь        │
└────────────────┘   └────────────────────────┘
 run: мгновенный      build: машинный код
 старт и отладка
```

`bootstrap/mandela.py` — временные «строительные леса»: минимальный
интерпретатор, существующий только чтобы запустить самохостящийся компилятор.
Он будет убран, когда Mandela начнёт генерировать машинный код сама
(см. roadmap).

## CLI

```bash
./bin/mandela run file.mnd [args...]    # проверить и выполнить (интерпретатор)
./bin/mandela build file.mnd [-o out]   # проверить и собрать нативный бинарь
./bin/mandela build file.mnd --emit c   # дамп промежуточного представления
./bin/mandela build file.mnd --emit asm # ассемблерный листинг
./bin/mandela check file.mnd            # только строгая проверка
./bin/mandela lex file.mnd              # дамп токенов (отладка)
./bin/mandela ast file.mnd              # дамп AST в s-выражениях (отладка)
```

## Возможности v0.3

- Статическая типизация с выводом типов: `let x = 5`, `list[int]`,
  `dict[str, int]`
- Неизменяемость по умолчанию: `let` против `let mut`, `mut self` для методов
- Целые 64-битные числа (усечённое деление, переполнение по модулю 2^64),
  строки, `bool`, `nil`, списки, словари с сохранением порядка вставки
- Строковые методы: `upper lower trim split contains starts_with ends_with
  replace find substr repeat chars`
- Методы словарей: `keys values has remove`
- Структуры с полями и методами, `self`, дисциплина `mut self`
- `if/elif/else`, `while`, `for x in range(...)/список/строка/словарь`,
  `break`, `continue`
- Функции с обязательными аннотациями параметров, рекурсия, хоистинг функций
- Встроенные: `print len str int chr ord push pop split substr read_file
  write_file args fatal exit range`
- Кодогенерация: нативные бинари через системный тулчейн, `--emit c|asm|bin`
- Строгий тайп-чекер с диагностиками `E0xxx`, предупреждения о неиспользуемых
  переменных

## Roadmap

| Версия | Что планируется |
| --- | --- |
| v0.4 | Собственный аллокатор и арены, `ptr<T>`, `unsafe`-блоки |
| v0.5 | Числа с плавающей точкой |
| v0.6 | Прямой бэкенд в машинный код (x86-64/ARM64) без промежуточного представления |
| v0.7 | Модули вместо линейных скриптов, менеджер пакетов |

## Разработка

```bash
make test        # 50 тестов: позитивные, негативные и паритет нативной сборки
```

Каждый позитивный тест прогоняется дважды: интерпретатором и в скомпилированном
виде — вывод обязан совпасть байт-в-байт.

Структура репозитория:

```text
bin/mandela          CLI
bootstrap/mandela.py бутстрап-движок (интерпретатор)
bootstrap/codegen.py бэкенд кодогенерации
src/*.mnd            компилятор, написанный на Mandela
spec/SPEC.md         спецификация языка v0.3
examples/            примеры программ
tests/               тесты (позитивные, негативные, нативные)
```

## Лицензия

[GPL-3.0](LICENSE) © mel0k1
