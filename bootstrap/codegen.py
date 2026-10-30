#!/usr/bin/env python3
"""Mandela C backend: lowers the bootstrap AST to portable C and builds it.

`mandela build program.mnd` = type-check (self-hosted compiler) -> lower to a
single self-contained C source -> invoke the system C toolchain -> native
executable. The runtime is built around a handful of C structs (MndValue,
MndStr, MndList, MndStruct, MndDict); user structs become C struct definition
tables, methods become plain functions dispatched by name at runtime.

Evaluation order and panic messages mirror the bootstrap interpreter exactly.
"""

import os
import shutil
import subprocess
import sys
import tempfile

import mandela


def c_str(s):
    """Renders a Mandela string as a C string literal plus its byte length."""
    raw = s.encode("utf-8")
    out = []
    for byte in raw:
        c = chr(byte)
        if c == '"':
            out.append('\\"')
        elif c == "\\":
            out.append("\\\\")
        elif 0x20 <= byte <= 0x7E:
            out.append(c)
        else:
            out.append("\\%03o" % byte)
    return '"%s"' % "".join(out), len(raw)


RUNTIME_C = r'''/* ---- Mandela runtime (v0.3) ---------------------------------- */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>
#include <ctype.h>

typedef struct MndStr MndStr;
typedef struct MndList MndList;
typedef struct MndStruct MndStruct;
typedef struct MndStructDef MndStructDef;
typedef struct MndDict MndDict;
typedef struct MndValue MndValue;
typedef struct MndArena MndArena;
typedef struct MndPtr MndPtr;

typedef enum { MND_NIL = 0, MND_INT, MND_BOOL, MND_STR, MND_LIST, MND_STRUCT, MND_DICT, MND_PTR, MND_ARENA } MndTag;

struct MndValue {
    MndTag tag;
    union {
        int64_t i;
        int b;
        MndStr *s;
        MndList *l;
        MndStruct *st;
        MndDict *d;
        MndPtr *pt;
        MndArena *ar;
    } u;
};

struct MndStr { int64_t len; char *data; };
struct MndList { int64_t len; int64_t cap; MndValue *items; };

struct MndArena {
    int freed;
    int64_t total;
    char **chunks;
    int64_t nchunks;
    int64_t ccap;
    char *cur;
    int64_t left;
};

struct MndPtr { MndValue *slots; int64_t len; int64_t off; MndArena *owner; };

static MndValue mnd_ptr_off(MndPtr *p, int64_t d);
static void mnd_ptr_check(MndPtr *p);

typedef MndValue (*MndMethodFn)(MndValue recv, int64_t n, const MndValue *args);
typedef struct MndMethodEntry { const char *name; MndMethodFn fn; } MndMethodEntry;

struct MndStructDef {
    const char *name;
    int64_t nfields;
    const char *const *field_names;
    int64_t nmethods;
    const MndMethodEntry *methods;
};

struct MndStruct { const MndStructDef *def; MndValue *fields; };

typedef struct { MndValue key; MndValue val; int used; } MndEntry;
struct MndDict {
    int64_t len;   /* live entries */
    int64_t nent;  /* slots used in entries[] (incl. tombstones) */
    int64_t ecap;  /* entries[] capacity */
    int64_t mask;  /* index table size - 1 */
    int32_t *index;
    MndEntry *entries;
};

static MndValue mnd_g_args;

static void mnd_panic(const char *msg) {
    fprintf(stderr, "panic: %s\n", msg);
    exit(101);
}

static void *mnd_alloc(size_t n) {
    void *p = malloc(n ? n : 1);
    if (p == NULL) mnd_panic("out of memory");
    return p;
}

static const char *mnd_type_name(MndValue v) {
    switch (v.tag) {
    case MND_NIL: return "nil";
    case MND_BOOL: return "bool";
    case MND_INT: return "int";
    case MND_STR: return "str";
    case MND_LIST: return "list";
    case MND_DICT: return "dict";
    case MND_PTR: return "ptr";
    case MND_ARENA: return "arena";
    case MND_STRUCT: return v.u.st->def->name;
    }
    return "?";
}

static MndValue mnd_nil_v(void) { MndValue v; v.tag = MND_NIL; v.u.i = 0; return v; }
static MndValue mnd_int_v(int64_t i) { MndValue v; v.tag = MND_INT; v.u.i = i; return v; }
static MndValue mnd_bool_v(int b) { MndValue v; v.tag = MND_BOOL; v.u.b = b ? 1 : 0; return v; }
static MndValue mnd_ptr_v(int tag, void *p) { MndValue v; v.tag = (MndTag)tag; v.u.s = p; return v; }
static MndValue mnd_ptr_val(MndPtr *p) { MndValue v; v.tag = MND_PTR; v.u.pt = p; return v; }
static MndValue mnd_arena_v(MndArena *a) { MndValue v; v.tag = MND_ARENA; v.u.ar = a; return v; }

static MndStr *mnd_str_new(const char *data, int64_t len) {
    MndStr *s = mnd_alloc(sizeof(MndStr));
    s->len = len;
    s->data = mnd_alloc((size_t)len + 1);
    if (len > 0) memcpy(s->data, data, (size_t)len);
    s->data[len] = 0;
    return s;
}

static MndValue mnd_str_lit(const char *z, int64_t n) {
    return mnd_ptr_v(MND_STR, mnd_str_new(z, n));
}

static MndValue mnd_str_v(const char *z) {
    return mnd_ptr_v(MND_STR, mnd_str_new(z, (int64_t)strlen(z)));
}

typedef struct { char *data; int64_t len; int64_t cap; } MndBuf;

static void mnd_buf_need(MndBuf *b, int64_t extra) {
    if (b->len + extra + 1 > b->cap) {
        int64_t nc = b->cap ? b->cap * 2 : 64;
        while (b->len + extra + 1 > nc) nc *= 2;
        char *nd = mnd_alloc((size_t)nc);
        if (b->len > 0) memcpy(nd, b->data, (size_t)b->len);
        b->data = nd;
        b->cap = nc;
    }
}

static void mnd_buf_put(MndBuf *b, const char *s, int64_t n) {
    mnd_buf_need(b, n);
    memcpy(b->data + b->len, s, (size_t)n);
    b->len += n;
}

static void mnd_buf_cstr(MndBuf *b, const char *s) { mnd_buf_put(b, s, (int64_t)strlen(s)); }

static void mnd_buf_i64(MndBuf *b, int64_t x) {
    char tmp[32];
    int n = snprintf(tmp, sizeof(tmp), "%lld", (long long)x);
    mnd_buf_put(b, tmp, n);
}

static char *mnd_buf_z(MndBuf *b) {
    mnd_buf_need(b, 0);
    b->data[b->len] = 0;
    return b->data;
}

static void mnd_format_into(MndBuf *b, MndValue v, int nested);

static void mnd_buf_str_escaped(MndBuf *b, MndStr *s) {
    for (int64_t i = 0; i < s->len; i++) {
        char c = s->data[i];
        if (c == '\\') mnd_buf_put(b, "\\\\", 2);
        else if (c == '"') mnd_buf_put(b, "\\\"", 2);
        else if (c == '\n') mnd_buf_put(b, "\\n", 2);
        else if (c == '\t') mnd_buf_put(b, "\\t", 2);
        else mnd_buf_put(b, &c, 1);
    }
}

static void mnd_format_into(MndBuf *b, MndValue v, int nested) {
    switch (v.tag) {
    case MND_NIL:
        mnd_buf_cstr(b, "nil");
        break;
    case MND_BOOL:
        mnd_buf_cstr(b, v.u.b ? "true" : "false");
        break;
    case MND_INT:
        mnd_buf_i64(b, v.u.i);
        break;
    case MND_STR:
        if (nested) {
            mnd_buf_put(b, "\"", 1);
            mnd_buf_str_escaped(b, v.u.s);
            mnd_buf_put(b, "\"", 1);
        } else {
            mnd_buf_put(b, v.u.s->data, v.u.s->len);
        }
        break;
    case MND_LIST:
        mnd_buf_put(b, "[", 1);
        for (int64_t i = 0; i < v.u.l->len; i++) {
            if (i > 0) mnd_buf_cstr(b, ", ");
            mnd_format_into(b, v.u.l->items[i], 1);
        }
        mnd_buf_put(b, "]", 1);
        break;
    case MND_STRUCT: {
        const MndStructDef *def = v.u.st->def;
        mnd_buf_cstr(b, def->name);
        mnd_buf_cstr(b, " { ");
        for (int64_t i = 0; i < def->nfields; i++) {
            if (i > 0) mnd_buf_cstr(b, ", ");
            mnd_buf_cstr(b, def->field_names[i]);
            mnd_buf_cstr(b, ": ");
            mnd_format_into(b, v.u.st->fields[i], 1);
        }
        mnd_buf_cstr(b, " }");
        break;
    }
    case MND_DICT: {
        mnd_buf_put(b, "{", 1);
        int first = 1;
        for (int64_t i = 0; i < v.u.d->nent; i++) {
            MndEntry *e = &v.u.d->entries[i];
            if (!e->used) continue;
            if (!first) mnd_buf_cstr(b, ", ");
            first = 0;
            mnd_format_into(b, e->key, 1);
            mnd_buf_cstr(b, ": ");
            mnd_format_into(b, e->val, 1);
        }
        mnd_buf_put(b, "}", 1);
        break;
    }
    case MND_PTR:
        mnd_buf_cstr(b, "<ptr>");
        break;
    case MND_ARENA:
        mnd_buf_cstr(b, "<arena>");
        break;
    }
}

static MndStr *mnd_format(MndValue v, int nested) {
    MndBuf b = {NULL, 0, 0};
    mnd_format_into(&b, v, nested);
    return mnd_str_new(b.data ? b.data : "", b.len);
}

/* ---- dict ------------------------------------------------------ */

static uint64_t mnd_hash_mix(uint64_t x) {
    x += 0x9E3779B97F4A7C15ULL;
    x = (x ^ (x >> 30)) * 0xBF58476D1CE4E5B9ULL;
    x = (x ^ (x >> 27)) * 0x94D049BB133111EBULL;
    return x ^ (x >> 31);
}

static uint64_t mnd_hash(MndValue v) {
    switch (v.tag) {
    case MND_NIL: return 0;
    case MND_BOOL: return mnd_hash_mix((uint64_t)v.u.b);
    case MND_INT: return mnd_hash_mix((uint64_t)v.u.i);
    case MND_STR: {
        uint64_t h = 1469598103934665603ULL;
        for (int64_t i = 0; i < v.u.s->len; i++) {
            h ^= (unsigned char)v.u.s->data[i];
            h *= 1099511628211ULL;
        }
        return h;
    }
    default: return mnd_hash_mix((uint64_t)(uintptr_t)v.u.s);
    }
}

static MndDict *mnd_dict_new(void) {
    MndDict *d = mnd_alloc(sizeof(MndDict));
    d->len = 0;
    d->nent = 0;
    d->ecap = 0;
    d->mask = 7;
    d->index = mnd_alloc(sizeof(int32_t) * 8);
    for (int i = 0; i < 8; i++) d->index[i] = -1;
    d->entries = NULL;
    return d;
}

static int mnd_eq(MndValue a, MndValue b);

static int64_t mnd_dict_find(MndDict *d, MndValue key) {
    uint64_t h = mnd_hash(key);
    int64_t i = (int64_t)(h & (uint64_t)d->mask);
    while (1) {
        int32_t slot = d->index[i];
        if (slot < 0) return -1;
        MndEntry *e = &d->entries[slot];
        if (e->used && mnd_eq(e->key, key)) return slot;
        i = (i + 1) & (int64_t)d->mask;
    }
}

static void mnd_dict_rebuild(MndDict *d) {
    int64_t nsize = (d->mask + 1) * 2;
    int32_t *ni = mnd_alloc(sizeof(int32_t) * (size_t)nsize);
    for (int64_t i = 0; i < nsize; i++) ni[i] = -1;
    d->mask = nsize - 1;
    for (int64_t i = 0; i < d->nent; i++) {
        MndEntry *e = &d->entries[i];
        if (!e->used) continue;
        uint64_t h = mnd_hash(e->key);
        int64_t j = (int64_t)(h & (uint64_t)d->mask);
        while (ni[j] >= 0) j = (j + 1) & (int64_t)d->mask;
        ni[j] = (int32_t)i;
    }
    d->index = ni;
}

static void mnd_dict_set(MndDict *d, MndValue key, MndValue val) {
    if (key.tag != MND_INT && key.tag != MND_STR && key.tag != MND_BOOL) {
        char msg[160];
        snprintf(msg, sizeof(msg), "dict key must be `str` or `int`, found `%s`",
                 mnd_type_name(key));
        mnd_panic(msg);
    }
    int64_t slot = mnd_dict_find(d, key);
    if (slot >= 0) {
        d->entries[slot].val = val;
        return;
    }
    if (d->nent >= d->ecap) {
        int64_t nc = d->ecap ? d->ecap * 2 : 8;
        MndEntry *ne = mnd_alloc(sizeof(MndEntry) * (size_t)nc);
        for (int64_t i = 0; i < d->nent; i++) ne[i] = d->entries[i];
        d->entries = ne;
        d->ecap = nc;
    }
    d->entries[d->nent].key = key;
    d->entries[d->nent].val = val;
    d->entries[d->nent].used = 1;
    int64_t idx = d->nent;
    d->nent += 1;
    d->len += 1;
    if ((d->nent + 1) * 10 > (d->mask + 1) * 7) mnd_dict_rebuild(d);
    uint64_t h = mnd_hash(key);
    int64_t i = (int64_t)(h & (uint64_t)d->mask);
    while (d->index[i] >= 0 && d->entries[d->index[i]].used)
        i = (i + 1) & (int64_t)d->mask;
    d->index[i] = (int32_t)idx;
}

static MndValue mnd_dict_get(MndDict *d, MndValue key) {
    if (key.tag != MND_INT && key.tag != MND_STR && key.tag != MND_BOOL) {
        char msg[160];
        snprintf(msg, sizeof(msg), "dict key must be `str` or `int`, found `%s`",
                 mnd_type_name(key));
        mnd_panic(msg);
    }
    int64_t slot = mnd_dict_find(d, key);
    if (slot < 0) {
        MndStr *ks = mnd_format(key, 0);
        MndBuf b = {NULL, 0, 0};
        mnd_buf_cstr(&b, "no key `");
        mnd_buf_put(&b, ks->data, ks->len);
        mnd_buf_cstr(&b, "` in dict");
        mnd_panic(mnd_buf_z(&b));
    }
    return d->entries[slot].val;
}

static int mnd_dict_has(MndDict *d, MndValue key) {
    if (key.tag != MND_INT && key.tag != MND_STR && key.tag != MND_BOOL) {
        char msg[160];
        snprintf(msg, sizeof(msg), "dict key must be `str` or `int`, found `%s`",
                 mnd_type_name(key));
        mnd_panic(msg);
    }
    return mnd_dict_find(d, key) >= 0;
}

static int mnd_dict_remove(MndDict *d, MndValue key) {
    if (key.tag != MND_INT && key.tag != MND_STR && key.tag != MND_BOOL) {
        char msg[160];
        snprintf(msg, sizeof(msg), "dict key must be `str` or `int`, found `%s`",
                 mnd_type_name(key));
        mnd_panic(msg);
    }
    int64_t slot = mnd_dict_find(d, key);
    if (slot < 0) return 0;
    d->entries[slot].used = 0;
    d->len -= 1;
    return 1;
}

static MndValue mnd_dict_keys(MndDict *d) {
    MndValue out = mnd_ptr_v(MND_LIST, mnd_alloc(sizeof(MndList)));
    MndList *l = out.u.l;
    l->len = 0;
    l->cap = d->len > 0 ? d->len : 0;
    l->items = d->len > 0 ? mnd_alloc(sizeof(MndValue) * (size_t)d->len) : NULL;
    for (int64_t i = 0; i < d->nent; i++) {
        if (d->entries[i].used) l->items[l->len++] = d->entries[i].key;
    }
    return out;
}

static MndValue mnd_dict_values(MndDict *d) {
    MndValue out = mnd_ptr_v(MND_LIST, mnd_alloc(sizeof(MndList)));
    MndList *l = out.u.l;
    l->len = 0;
    l->cap = d->len > 0 ? d->len : 0;
    l->items = d->len > 0 ? mnd_alloc(sizeof(MndValue) * (size_t)d->len) : NULL;
    for (int64_t i = 0; i < d->nent; i++) {
        if (d->entries[i].used) l->items[l->len++] = d->entries[i].val;
    }
    return out;
}

/* ---- equality --------------------------------------------------- */

static int mnd_eq(MndValue a, MndValue b) {
    if (a.tag != b.tag) return 0;
    switch (a.tag) {
    case MND_NIL: return 1;
    case MND_BOOL: return a.u.b == b.u.b;
    case MND_INT: return a.u.i == b.u.i;
    case MND_STR:
        return a.u.s->len == b.u.s->len &&
               memcmp(a.u.s->data, b.u.s->data, (size_t)a.u.s->len) == 0;
    case MND_LIST:
        if (a.u.l->len != b.u.l->len) return 0;
        for (int64_t i = 0; i < a.u.l->len; i++)
            if (!mnd_eq(a.u.l->items[i], b.u.l->items[i])) return 0;
        return 1;
    case MND_STRUCT:
        if (a.u.st->def != b.u.st->def) return 0;
        for (int64_t i = 0; i < a.u.st->def->nfields; i++)
            if (!mnd_eq(a.u.st->fields[i], b.u.st->fields[i])) return 0;
        return 1;
    case MND_DICT:
        if (a.u.d->len != b.u.d->len) return 0;
        for (int64_t i = 0; i < a.u.d->nent; i++) {
            MndEntry *e = &a.u.d->entries[i];
            if (!e->used) continue;
            if (!mnd_dict_has(b.u.d, e->key)) return 0;
            if (!mnd_eq(e->val, mnd_dict_get(b.u.d, e->key))) return 0;
        }
        return 1;
    case MND_PTR:
        return a.u.pt->slots == b.u.pt->slots && a.u.pt->off == b.u.pt->off;
    case MND_ARENA:
        return a.u.ar == b.u.ar;
    }
    return 0;
}

static int mnd_truthy(MndValue v) {
    if (v.tag != MND_BOOL) {
        char msg[192];
        snprintf(msg, sizeof(msg), "expected `bool`, found `%s`", mnd_type_name(v));
        mnd_panic(msg);
    }
    return v.u.b;
}

static MndValue mnd_not(MndValue v) { return mnd_bool_v(!mnd_truthy(v)); }

/* ---- arithmetic -------------------------------------------------- */

static MndValue mnd_arith_fail(const char *op, MndValue l, MndValue r) {
    char msg[224];
    snprintf(msg, sizeof(msg), "cannot apply operator `%s` to types `%s` and `%s`",
             op, mnd_type_name(l), mnd_type_name(r));
    mnd_panic(msg);
    return mnd_nil_v();
}

static MndValue mnd_add(MndValue l, MndValue r) {
    if (l.tag == MND_STR && r.tag == MND_STR) {
        MndStr *s = mnd_str_new("", l.u.s->len + r.u.s->len);
        if (l.u.s->len > 0) memcpy(s->data, l.u.s->data, (size_t)l.u.s->len);
        if (r.u.s->len > 0) memcpy(s->data + l.u.s->len, r.u.s->data, (size_t)r.u.s->len);
        return mnd_ptr_v(MND_STR, s);
    }
    if (l.tag == MND_PTR && r.tag == MND_INT) return mnd_ptr_off(l.u.pt, r.u.i);
    if (l.tag != MND_INT || r.tag != MND_INT) return mnd_arith_fail("+", l, r);
    return mnd_int_v((int64_t)((uint64_t)l.u.i + (uint64_t)r.u.i));
}

static MndValue mnd_sub(MndValue l, MndValue r) {
    if (l.tag == MND_PTR && r.tag == MND_INT) return mnd_ptr_off(l.u.pt, (int64_t)(0 - (uint64_t)r.u.i));
    if (l.tag != MND_INT || r.tag != MND_INT) return mnd_arith_fail("-", l, r);
    return mnd_int_v((int64_t)((uint64_t)l.u.i - (uint64_t)r.u.i));
}

static MndValue mnd_mul(MndValue l, MndValue r) {
    if (l.tag != MND_INT || r.tag != MND_INT) return mnd_arith_fail("*", l, r);
    return mnd_int_v((int64_t)((uint64_t)l.u.i * (uint64_t)r.u.i));
}

static MndValue mnd_div(MndValue l, MndValue r) {
    if (l.tag != MND_INT || r.tag != MND_INT) return mnd_arith_fail("/", l, r);
    if (r.u.i == 0) mnd_panic("attempt to divide by zero");
    if (r.u.i == -1) return mnd_int_v((int64_t)(0 - (uint64_t)l.u.i));
    return mnd_int_v(l.u.i / r.u.i);
}

static MndValue mnd_rem(MndValue l, MndValue r) {
    if (l.tag != MND_INT || r.tag != MND_INT) return mnd_arith_fail("%", l, r);
    if (r.u.i == 0) mnd_panic("attempt to calculate the remainder with a divisor of zero");
    if (r.u.i == -1) return mnd_int_v(0);
    return mnd_int_v(l.u.i % r.u.i);
}

static MndValue mnd_neg(MndValue v) {
    if (v.tag != MND_INT) {
        char msg[192];
        snprintf(msg, sizeof(msg), "cannot apply unary `-` to type `%s`", mnd_type_name(v));
        mnd_panic(msg);
    }
    return mnd_int_v((int64_t)(0 - (uint64_t)v.u.i));
}

/* ---- comparison -------------------------------------------------- */

static MndValue mnd_cmp_fail(MndValue l, MndValue r) {
    char msg[224];
    snprintf(msg, sizeof(msg), "cannot compare `%s` with `%s`",
             mnd_type_name(l), mnd_type_name(r));
    mnd_panic(msg);
    return mnd_nil_v();
}

static int mnd_str_cmp(MndStr *a, MndStr *b) {
    int64_t n = a->len < b->len ? a->len : b->len;
    int c = n > 0 ? memcmp(a->data, b->data, (size_t)n) : 0;
    if (c != 0) return c < 0;
    return a->len < b->len;
}

static MndValue mnd_lt(MndValue l, MndValue r) {
    if (l.tag == MND_INT && r.tag == MND_INT) return mnd_bool_v(l.u.i < r.u.i);
    if (l.tag == MND_STR && r.tag == MND_STR) return mnd_bool_v(mnd_str_cmp(l.u.s, r.u.s));
    return mnd_cmp_fail(l, r);
}

static MndValue mnd_le(MndValue l, MndValue r) {
    if (l.tag == MND_INT && r.tag == MND_INT) return mnd_bool_v(l.u.i <= r.u.i);
    if (l.tag == MND_STR && r.tag == MND_STR)
        return mnd_bool_v(!mnd_str_cmp(r.u.s, l.u.s));
    return mnd_cmp_fail(l, r);
}

static MndValue mnd_gt(MndValue l, MndValue r) {
    if (l.tag == MND_INT && r.tag == MND_INT) return mnd_bool_v(l.u.i > r.u.i);
    if (l.tag == MND_STR && r.tag == MND_STR) return mnd_bool_v(mnd_str_cmp(r.u.s, l.u.s));
    return mnd_cmp_fail(l, r);
}

static MndValue mnd_ge(MndValue l, MndValue r) {
    if (l.tag == MND_INT && r.tag == MND_INT) return mnd_bool_v(l.u.i >= r.u.i);
    if (l.tag == MND_STR && r.tag == MND_STR)
        return mnd_bool_v(!mnd_str_cmp(l.u.s, r.u.s));
    return mnd_cmp_fail(l, r);
}

/* ---- structures --------------------------------------------------- */

static int64_t mnd_field_index(const MndStructDef *def, const char *name) {
    for (int64_t i = 0; i < def->nfields; i++)
        if (strcmp(def->field_names[i], name) == 0) return i;
    return -1;
}

static MndValue mnd_struct_lit(const MndStructDef *def, int64_t n,
                               const char *const *names, const MndValue *vals) {
    MndStruct *st = mnd_alloc(sizeof(MndStruct));
    st->def = def;
    st->fields = mnd_alloc(sizeof(MndValue) * (size_t)(def->nfields > 0 ? def->nfields : 1));
    for (int64_t i = 0; i < def->nfields; i++) st->fields[i] = mnd_nil_v();
    for (int64_t j = 0; j < n; j++) {
        int64_t i = mnd_field_index(def, names[j]);
        if (i < 0) {
            char msg[192];
            snprintf(msg, sizeof(msg), "struct `%s` has no field named `%s`", def->name, names[j]);
            mnd_panic(msg);
        }
        st->fields[i] = vals[j];
    }
    return mnd_ptr_v(MND_STRUCT, st);
}

static MndValue mnd_struct_get(MndValue v, const char *name) {
    if (v.tag != MND_STRUCT) {
        char msg[192];
        snprintf(msg, sizeof(msg), "no field `%s` on type `%s`", name, mnd_type_name(v));
        mnd_panic(msg);
    }
    int64_t i = mnd_field_index(v.u.st->def, name);
    if (i < 0) {
        char msg[192];
        snprintf(msg, sizeof(msg), "no field `%s` on type `%s`", name, v.u.st->def->name);
        mnd_panic(msg);
    }
    return v.u.st->fields[i];
}

static void mnd_struct_set(MndValue v, const char *name, MndValue val) {
    if (v.tag != MND_STRUCT) {
        char msg[192];
        snprintf(msg, sizeof(msg), "no field `%s` on type `%s`", name, mnd_type_name(v));
        mnd_panic(msg);
    }
    int64_t i = mnd_field_index(v.u.st->def, name);
    if (i < 0) {
        char msg[192];
        snprintf(msg, sizeof(msg), "no field `%s` on type `%s`", name, v.u.st->def->name);
        mnd_panic(msg);
    }
    v.u.st->fields[i] = val;
}

/* ---- lists and indexing ------------------------------------------- */

static MndValue mnd_list_lit(int64_t n, const MndValue *items) {
    MndList *l = mnd_alloc(sizeof(MndList));
    l->len = n;
    l->cap = n > 0 ? n : 0;
    l->items = n > 0 ? mnd_alloc(sizeof(MndValue) * (size_t)n) : NULL;
    for (int64_t i = 0; items != NULL && i < n; i++) l->items[i] = items[i];
    return mnd_ptr_v(MND_LIST, l);
}

static MndValue mnd_dict_lit(int64_t n, const MndValue *items) {
    MndDict *d = mnd_dict_new();
    for (int64_t i = 0; i + 1 < n; i += 2) mnd_dict_set(d, items[i], items[i + 1]);
    return mnd_ptr_v(MND_DICT, d);
}

static int64_t mnd_as_i64(MndValue v) {
    if (v.tag != MND_INT) {
        char msg[192];
        snprintf(msg, sizeof(msg), "expected `int`, found `%s`", mnd_type_name(v));
        mnd_panic(msg);
    }
    return v.u.i;
}

static void mnd_panic_index(int64_t n, int64_t i) {
    char msg[96];
    snprintf(msg, sizeof(msg), "index out of bounds: the len is %lld but the index is %lld",
             (long long)n, (long long)i);
    mnd_panic(msg);
}

static int64_t mnd_require_index(MndValue v) {
    if (v.tag != MND_INT) {
        char msg[192];
        snprintf(msg, sizeof(msg), "index must be `int`, found `%s`", mnd_type_name(v));
        mnd_panic(msg);
    }
    return v.u.i;
}

static MndValue mnd_str_char(MndStr *s, int64_t i) {
    return mnd_ptr_v(MND_STR, mnd_str_new(s->data + i, 1));
}

static MndValue mnd_index_get(MndValue base, MndValue key) {
    if (base.tag == MND_DICT) {
        if (key.tag != MND_INT && key.tag != MND_STR && key.tag != MND_BOOL) {
            char msg[160];
            snprintf(msg, sizeof(msg), "dict key must be `str` or `int`, found `%s`",
                     mnd_type_name(key));
            mnd_panic(msg);
        }
        return mnd_dict_get(base.u.d, key);
    }
    int64_t i = mnd_require_index(key);
    if (base.tag == MND_PTR) {
        MndPtr *p = base.u.pt;
        mnd_ptr_check(p);
        int64_t pos = p->off + i;
        if (pos < 0 || pos >= p->len) mnd_panic_index(p->len, pos);
        return p->slots[pos];
    }
    if (base.tag == MND_NIL) mnd_panic("null pointer access");
    if (base.tag == MND_STR) {
        if (i < 0 || i >= base.u.s->len) mnd_panic_index(base.u.s->len, i);
        return mnd_str_char(base.u.s, i);
    }
    if (base.tag == MND_LIST) {
        if (i < 0 || i >= base.u.l->len) mnd_panic_index(base.u.l->len, i);
        return base.u.l->items[i];
    }
    char msg[192];
    snprintf(msg, sizeof(msg), "expected a `list`, found `%s`", mnd_type_name(base));
    mnd_panic(msg);
    return mnd_nil_v();
}

static void mnd_index_set(MndValue base, MndValue key, MndValue val) {
    if (base.tag == MND_DICT) {
        if (key.tag != MND_INT && key.tag != MND_STR && key.tag != MND_BOOL) {
            char msg[160];
            snprintf(msg, sizeof(msg), "dict key must be `str` or `int`, found `%s`",
                     mnd_type_name(key));
            mnd_panic(msg);
        }
        mnd_dict_set(base.u.d, key, val);
        return;
    }
    int64_t i = mnd_require_index(key);
    if (base.tag == MND_PTR) {
        MndPtr *p = base.u.pt;
        mnd_ptr_check(p);
        int64_t pos = p->off + i;
        if (pos < 0 || pos >= p->len) mnd_panic_index(p->len, pos);
        p->slots[pos] = val;
        return;
    }
    if (base.tag == MND_STR)
        mnd_panic("strings are immutable: cannot assign to a string index");
    if (base.tag == MND_LIST) {
        if (i < 0 || i >= base.u.l->len) mnd_panic_index(base.u.l->len, i);
        base.u.l->items[i] = val;
        return;
    }
    char msg[192];
    snprintf(msg, sizeof(msg), "expected a `list`, found `%s`", mnd_type_name(base));
    mnd_panic(msg);
}

static MndValue mnd_iter_view(MndValue v) {
    if (v.tag == MND_STR || v.tag == MND_LIST) return v;
    if (v.tag == MND_DICT) return mnd_dict_keys(v.u.d);
    char msg[192];
    snprintf(msg, sizeof(msg), "expected a `list`, found `%s`", mnd_type_name(v));
    mnd_panic(msg);
    return mnd_nil_v();
}

static int64_t mnd_iter_len(MndValue v) {
    if (v.tag == MND_STR) return v.u.s->len;
    if (v.tag == MND_LIST) return v.u.l->len;
    char msg[192];
    snprintf(msg, sizeof(msg), "expected a `list`, found `%s`", mnd_type_name(v));
    mnd_panic(msg);
    return 0;
}

static MndValue mnd_iter_get(MndValue v, int64_t i) {
    if (v.tag == MND_STR) return mnd_str_char(v.u.s, i);
    return v.u.l->items[i];
}

/* ---- string methods ------------------------------------------------ */

static void mnd_m_arity(const char *name, int64_t want, int64_t got) {
    if (want == got) return;
    char msg[160];
    snprintf(msg, sizeof(msg),
             "%s() takes exactly %lld argument%s but %lld were given",
             name, (long long)want, want == 1 ? "" : "s", (long long)got);
    mnd_panic(msg);
}

static MndValue mnd_sm_upper(MndValue recv, int64_t n, const MndValue *args) {
    (void)n; (void)args;
    MndStr *s = recv.u.s;
    MndStr *r = mnd_str_new("", s->len);
    for (int64_t i = 0; i < s->len; i++) r->data[i] = (char)toupper((unsigned char)s->data[i]);
    return mnd_ptr_v(MND_STR, r);
}

static MndValue mnd_sm_lower(MndValue recv, int64_t n, const MndValue *args) {
    (void)n; (void)args;
    MndStr *s = recv.u.s;
    MndStr *r = mnd_str_new("", s->len);
    for (int64_t i = 0; i < s->len; i++) r->data[i] = (char)tolower((unsigned char)s->data[i]);
    return mnd_ptr_v(MND_STR, r);
}

static MndValue mnd_sm_trim(MndValue recv, int64_t n, const MndValue *args) {
    (void)n; (void)args;
    MndStr *s = recv.u.s;
    int64_t a = 0;
    int64_t b = s->len;
    while (a < b && isspace((unsigned char)s->data[a])) a++;
    while (b > a && isspace((unsigned char)s->data[b - 1])) b--;
    return mnd_ptr_v(MND_STR, mnd_str_new(s->data + a, b - a));
}

static int64_t mnd_str_find(MndStr *s, MndStr *sub) {
    if (sub->len > s->len) return -1;
    for (int64_t i = 0; i + sub->len <= s->len; i++) {
        if (memcmp(s->data + i, sub->data, (size_t)sub->len) == 0) return i;
    }
    return -1;
}

static MndValue mnd_sm_split(MndValue recv, int64_t n, const MndValue *args) {
    MndStr *s = recv.u.s;
    if (n != 1) {
        char msg[160];
        snprintf(msg, sizeof(msg), "split() takes exactly 2 arguments but %lld were given",
                 (long long)(n + 1));
        mnd_panic(msg);
    }
    if (args[0].tag != MND_STR) mnd_panic("split() expects two `str` arguments");
    MndStr *sep = args[0].u.s;
    if (sep->len == 0) mnd_panic("split() separator must not be empty");
    MndValue out = mnd_list_lit(0, NULL);
    MndList *l = out.u.l;
    int64_t start = 0;
    for (int64_t i = 0; i + sep->len <= s->len; ) {
        if (memcmp(s->data + i, sep->data, (size_t)sep->len) == 0) {
            MndValue part = mnd_ptr_v(MND_STR, mnd_str_new(s->data + start, i - start));
            if (l->len >= l->cap) {
                int64_t nc = l->cap ? l->cap * 2 : 8;
                MndValue *ni = mnd_alloc(sizeof(MndValue) * (size_t)nc);
                for (int64_t k = 0; k < l->len; k++) ni[k] = l->items[k];
                l->items = ni;
                l->cap = nc;
            }
            l->items[l->len++] = part;
            i += sep->len;
            start = i;
        } else {
            i += 1;
        }
    }
    MndValue tail = mnd_ptr_v(MND_STR, mnd_str_new(s->data + start, s->len - start));
    if (l->len >= l->cap) {
        int64_t nc = l->cap ? l->cap * 2 : 8;
        MndValue *ni = mnd_alloc(sizeof(MndValue) * (size_t)nc);
        for (int64_t k = 0; k < l->len; k++) ni[k] = l->items[k];
        l->items = ni;
        l->cap = nc;
    }
    l->items[l->len++] = tail;
    return out;
}

static MndValue mnd_sm_contains(MndValue recv, int64_t n, const MndValue *args) {
    mnd_m_arity("contains", 1, n);
    if (args[0].tag != MND_STR) mnd_panic("contains() expects a `str` argument");
    return mnd_bool_v(mnd_str_find(recv.u.s, args[0].u.s) >= 0);
}

static MndValue mnd_sm_starts_with(MndValue recv, int64_t n, const MndValue *args) {
    mnd_m_arity("starts_with", 1, n);
    if (args[0].tag != MND_STR) mnd_panic("starts_with() expects a `str` argument");
    MndStr *s = recv.u.s;
    MndStr *p = args[0].u.s;
    if (p->len > s->len) return mnd_bool_v(0);
    return mnd_bool_v(memcmp(s->data, p->data, (size_t)p->len) == 0);
}

static MndValue mnd_sm_ends_with(MndValue recv, int64_t n, const MndValue *args) {
    mnd_m_arity("ends_with", 1, n);
    if (args[0].tag != MND_STR) mnd_panic("ends_with() expects a `str` argument");
    MndStr *s = recv.u.s;
    MndStr *p = args[0].u.s;
    if (p->len > s->len) return mnd_bool_v(0);
    return mnd_bool_v(memcmp(s->data + s->len - p->len, p->data, (size_t)p->len) == 0);
}

static MndValue mnd_sm_replace(MndValue recv, int64_t n, const MndValue *args) {
    mnd_m_arity("replace", 2, n);
    if (args[0].tag != MND_STR || args[1].tag != MND_STR)
        mnd_panic("replace() expects two `str` arguments");
    MndStr *s = recv.u.s;
    MndStr *old = args[0].u.s;
    MndStr *nw = args[1].u.s;
    if (old->len == 0) mnd_panic("replace() old must not be empty");
    MndBuf b = {NULL, 0, 0};
    int64_t i = 0;
    while (i < s->len) {
        if (i + old->len <= s->len && memcmp(s->data + i, old->data, (size_t)old->len) == 0) {
            mnd_buf_put(&b, nw->data, nw->len);
            i += old->len;
        } else {
            mnd_buf_put(&b, s->data + i, 1);
            i += 1;
        }
    }
    return mnd_ptr_v(MND_STR, mnd_str_new(b.data ? b.data : "", b.len));
}

static MndValue mnd_sm_find(MndValue recv, int64_t n, const MndValue *args) {
    mnd_m_arity("find", 1, n);
    if (args[0].tag != MND_STR) mnd_panic("find() expects a `str` argument");
    return mnd_int_v(mnd_str_find(recv.u.s, args[0].u.s));
}

static MndValue mnd_sm_substr(MndValue recv, int64_t n, const MndValue *args) {
    if (n != 2) {
        char msg[160];
        snprintf(msg, sizeof(msg), "substr() takes exactly 3 arguments but %lld were given",
                 (long long)(n + 1));
        mnd_panic(msg);
    }
    MndStr *s = recv.u.s;
    if (args[0].tag != MND_INT || args[1].tag != MND_INT)
        mnd_panic("substr() expects `int` offsets");
    int64_t start = args[0].u.i;
    int64_t ln = args[1].u.i;
    if (start < 0) start = 0;
    if (ln < 0) ln = 0;
    if (start > s->len) return mnd_str_lit("", 0);
    if (ln > s->len - start) ln = s->len - start;
    return mnd_ptr_v(MND_STR, mnd_str_new(s->data + start, ln));
}

static MndValue mnd_sm_repeat(MndValue recv, int64_t n, const MndValue *args) {
    mnd_m_arity("repeat", 1, n);
    if (args[0].tag != MND_INT) {
        char msg[192];
        snprintf(msg, sizeof(msg), "repeat() expects an `int` argument, found `%s`",
                 mnd_type_name(args[0]));
        mnd_panic(msg);
    }
    MndStr *s = recv.u.s;
    int64_t k = args[0].u.i;
    if (k <= 0) return mnd_str_lit("", 0);
    MndStr *r = mnd_str_new("", s->len * k);
    for (int64_t i = 0; i < k; i++) memcpy(r->data + i * s->len, s->data, (size_t)s->len);
    return mnd_ptr_v(MND_STR, r);
}

static MndValue mnd_sm_chars(MndValue recv, int64_t n, const MndValue *args) {
    (void)n; (void)args;
    MndStr *s = recv.u.s;
    MndValue out = mnd_list_lit(s->len, NULL);
    for (int64_t i = 0; i < s->len; i++) out.u.l->items[i] = mnd_str_char(s, i);
    return out;
}

/* ---- dict methods --------------------------------------------------- */

static MndValue mnd_dm_keys(MndValue recv, int64_t n, const MndValue *args) {
    (void)args;
    mnd_m_arity("keys", 0, n);
    return mnd_dict_keys(recv.u.d);
}

static MndValue mnd_dm_values(MndValue recv, int64_t n, const MndValue *args) {
    (void)args;
    mnd_m_arity("values", 0, n);
    return mnd_dict_values(recv.u.d);
}

static MndValue mnd_dm_has(MndValue recv, int64_t n, const MndValue *args) {
    mnd_m_arity("has", 1, n);
    return mnd_bool_v(mnd_dict_has(recv.u.d, args[0]));
}

static MndValue mnd_dm_remove(MndValue recv, int64_t n, const MndValue *args) {
    mnd_m_arity("remove", 1, n);
    return mnd_bool_v(mnd_dict_remove(recv.u.d, args[0]));
}

static const MndMethodEntry mnd_str_methods[] = {
    {"upper", mnd_sm_upper}, {"lower", mnd_sm_lower}, {"trim", mnd_sm_trim},
    {"split", mnd_sm_split}, {"contains", mnd_sm_contains},
    {"starts_with", mnd_sm_starts_with}, {"ends_with", mnd_sm_ends_with},
    {"replace", mnd_sm_replace}, {"find", mnd_sm_find},
    {"substr", mnd_sm_substr}, {"repeat", mnd_sm_repeat}, {"chars", mnd_sm_chars},
};

static const MndMethodEntry mnd_dict_methods[] = {
    {"keys", mnd_dm_keys}, {"values", mnd_dm_values},
    {"has", mnd_dm_has}, {"remove", mnd_dm_remove},
};

/* ---- method dispatch ------------------------------------------------- */

static MndValue mnd_call_method(MndValue recv, const char *name, int64_t n,
                                const MndValue *args) {
    const MndMethodEntry *tab = NULL;
    int64_t ntab = 0;
    int is_struct = 0;
    if (recv.tag == MND_STR) {
        tab = mnd_str_methods;
        ntab = (int64_t)(sizeof(mnd_str_methods) / sizeof(mnd_str_methods[0]));
    } else if (recv.tag == MND_DICT) {
        tab = mnd_dict_methods;
        ntab = (int64_t)(sizeof(mnd_dict_methods) / sizeof(mnd_dict_methods[0]));
    } else if (recv.tag == MND_STRUCT) {
        tab = recv.u.st->def->methods;
        ntab = recv.u.st->def->nmethods;
        is_struct = 1;
    }
    if (tab != NULL) {
        for (int64_t i = 0; i < ntab; i++)
            if (strcmp(tab[i].name, name) == 0) return tab[i].fn(recv, n, args);
    }
    char msg[224];
    if (is_struct)
        snprintf(msg, sizeof(msg), "no method `%s` on struct `%s`", name, recv.u.st->def->name);
    else
        snprintf(msg, sizeof(msg), "no method `%s` on type `%s`", name, mnd_type_name(recv));
    mnd_panic(msg);
    return mnd_nil_v();
}

static void mnd_panic_arity(const char *name, int64_t want, int64_t got) {
    char msg[160];
    snprintf(msg, sizeof(msg), "method `%s` takes %lld argument(s) but %lld were given",
             name, (long long)want, (long long)got);
    mnd_panic(msg);
}

/* ---- pointers and arenas ----------------------------------------------- */

static MndArena mnd_g_arena;

static void mnd_ptr_check(MndPtr *p) {
    if (p->owner != NULL && p->owner->freed) mnd_panic("use after arena_free");
}

static void *mnd_arena_alloc(MndArena *ar, size_t n) {
    n = (n + 15) & ~(size_t)15;
    if (n == 0) n = 16;
    if (ar->left < (int64_t)n) {
        size_t csz = (size_t)1 << 20;
        if (n > csz) csz = n;
        char *chunk = mnd_alloc(csz);
        if (ar->nchunks == ar->ccap) {
            ar->ccap = ar->ccap ? ar->ccap * 2 : 8;
            char **nc = mnd_alloc(sizeof(char *) * (size_t)ar->ccap);
            if (ar->nchunks > 0)
                memcpy(nc, ar->chunks, sizeof(char *) * (size_t)ar->nchunks);
            ar->chunks = nc;
        }
        ar->chunks[ar->nchunks++] = chunk;
        ar->cur = chunk;
        ar->left = (int64_t)csz;
    }
    void *p = ar->cur;
    ar->cur += n;
    ar->left -= (int64_t)n;
    return p;
}

static MndValue mnd_ptr_new(MndArena *ar, int64_t n, MndValue fill) {
    MndValue *slots = (MndValue *)mnd_arena_alloc(
        ar, sizeof(MndValue) * (size_t)(n > 0 ? n : 1));
    for (int64_t i = 0; i < n; i++) slots[i] = fill;
    MndPtr *p = mnd_alloc(sizeof(MndPtr));
    p->slots = slots;
    p->len = n;
    p->off = 0;
    p->owner = ar == &mnd_g_arena ? NULL : ar;
    return mnd_ptr_val(p);
}

static MndValue mnd_ptr_off(MndPtr *p, int64_t d) {
    MndPtr *q = mnd_alloc(sizeof(MndPtr));
    q->slots = p->slots;
    q->len = p->len;
    q->off = p->off + d;
    q->owner = p->owner;
    return mnd_ptr_val(q);
}

static int64_t mnd_len_arg(MndValue v, const char *fn) {
    if (v.tag != MND_INT) {
        char msg[192];
        snprintf(msg, sizeof(msg), "%s expects an `int` length, found `%s`",
                 fn, mnd_type_name(v));
        mnd_panic(msg);
    }
    return v.u.i;
}

static MndArena *mnd_arena_arg(MndValue v, const char *fn) {
    if (v.tag != MND_ARENA) {
        char msg[192];
        snprintf(msg, sizeof(msg), "%s expects an `Arena`, found `%s`",
                 fn, mnd_type_name(v));
        mnd_panic(msg);
    }
    return v.u.ar;
}

static MndPtr *mnd_ptr_arg(MndValue v, const char *fn) {
    if (v.tag == MND_NIL) mnd_panic("null pointer access");
    if (v.tag != MND_PTR) {
        char msg[192];
        snprintf(msg, sizeof(msg), "expected a `ptr` in `%s`, found `%s`",
                 fn, mnd_type_name(v));
        mnd_panic(msg);
    }
    return v.u.pt;
}

static MndValue mnd_bi_alloc(MndValue n, MndValue fill) {
    int64_t k = mnd_len_arg(n, "alloc()");
    if (k < 0) mnd_panic("ptr length must not be negative");
    return mnd_ptr_new(&mnd_g_arena, k, fill);
}

static MndValue mnd_bi_arena_new(void) {
    MndArena *a = mnd_alloc(sizeof(MndArena));
    a->freed = 0;
    a->total = 0;
    a->chunks = NULL;
    a->nchunks = 0;
    a->ccap = 0;
    a->cur = NULL;
    a->left = 0;
    return mnd_arena_v(a);
}

static MndValue mnd_bi_arena_alloc(MndValue a, MndValue n, MndValue fill) {
    MndArena *ar = mnd_arena_arg(a, "arena_alloc()");
    int64_t k = mnd_len_arg(n, "arena_alloc()");
    if (ar->freed) mnd_panic("use of a freed arena");
    if (k < 0) mnd_panic("ptr length must not be negative");
    ar->total += k * 16;
    return mnd_ptr_new(ar, k, fill);
}

static MndValue mnd_bi_arena_free(MndValue a) {
    MndArena *ar = mnd_arena_arg(a, "arena_free()");
    if (ar->freed) mnd_panic("double free of an arena");
    ar->freed = 1;
    for (int64_t i = 0; i < ar->nchunks; i++) free(ar->chunks[i]);
    ar->nchunks = 0;
    ar->chunks = NULL;
    ar->cur = NULL;
    ar->left = 0;
    return mnd_int_v(ar->total);
}

static MndValue mnd_bi_ptr_len(MndValue v) {
    MndPtr *p = mnd_ptr_arg(v, "ptr_len");
    mnd_ptr_check(p);
    return mnd_int_v(p->len);
}

static MndValue mnd_bi_is_null(MndValue v) {
    if (v.tag == MND_NIL) return mnd_bool_v(1);
    if (v.tag != MND_PTR) {
        char msg[192];
        snprintf(msg, sizeof(msg), "expected a `ptr` in `is_null`, found `%s`",
                 mnd_type_name(v));
        mnd_panic(msg);
    }
    return mnd_bool_v(0);
}

/* ---- builtins ---------------------------------------------------------- */

static MndValue mnd_bi_print(MndValue v) {
    MndStr *s = mnd_format(v, 0);
    fwrite(s->data, 1, (size_t)s->len, stdout);
    fputc('\n', stdout);
    return mnd_nil_v();
}

static MndValue mnd_bi_eprint(MndValue v) {
    MndStr *s = mnd_format(v, 0);
    fwrite(s->data, 1, (size_t)s->len, stderr);
    fputc('\n', stderr);
    return mnd_nil_v();
}

static MndValue mnd_bi_len(MndValue v) {
    if (v.tag == MND_STR) return mnd_int_v(v.u.s->len);
    if (v.tag == MND_LIST) return mnd_int_v(v.u.l->len);
    if (v.tag == MND_DICT) return mnd_int_v(v.u.d->len);
    char msg[192];
    snprintf(msg, sizeof(msg), "expected `str`, a list or a dict in len(), found `%s`",
             mnd_type_name(v));
    mnd_panic(msg);
    return mnd_nil_v();
}

static MndValue mnd_bi_str(MndValue v) {
    return mnd_ptr_v(MND_STR, mnd_format(v, 0));
}

static MndValue mnd_bi_int(MndValue v) {
    if (v.tag == MND_INT) return v;
    if (v.tag != MND_STR) {
        char msg[192];
        snprintf(msg, sizeof(msg), "expected `str` or `int` in int(), found `%s`",
                 mnd_type_name(v));
        mnd_panic(msg);
    }
    MndStr *s = v.u.s;
    int64_t i = 0;
    int neg = 0;
    if (s->len > 0 && (s->data[0] == '+' || s->data[0] == '-')) {
        neg = s->data[0] == '-';
        i = 1;
    }
    if (i >= s->len) mnd_panic("cannot parse integer from empty string");
    uint64_t acc = 0;
    uint64_t limit = neg ? ((uint64_t)1 << 63) : (uint64_t)INT64_MAX;
    for (; i < s->len; i++) {
        char c = s->data[i];
        if (c < '0' || c > '9') mnd_panic("invalid digit found in string");
        acc = acc * 10 + (uint64_t)(c - '0');
        if (acc > limit) mnd_panic("number too large to fit in target type");
    }
    return mnd_int_v(neg ? (int64_t)(0 - acc) : (int64_t)acc);
}

static MndValue mnd_bi_chr(MndValue v) {
    if (v.tag != MND_INT) {
        char msg[192];
        snprintf(msg, sizeof(msg), "expected `int` in chr(), found `%s`", mnd_type_name(v));
        mnd_panic(msg);
    }
    int64_t cp = v.u.i;
    if (cp < 0 || cp > 0x10FFFF || (cp >= 0xD800 && cp <= 0xDFFF)) {
        char msg[96];
        snprintf(msg, sizeof(msg), "invalid code point in chr(): %lld", (long long)cp);
        mnd_panic(msg);
    }
    char buf[4];
    int n;
    if (cp < 0x80) {
        buf[0] = (char)cp;
        n = 1;
    } else if (cp < 0x800) {
        buf[0] = (char)(0xC0 | (cp >> 6));
        buf[1] = (char)(0x80 | (cp & 0x3F));
        n = 2;
    } else if (cp < 0x10000) {
        buf[0] = (char)(0xE0 | (cp >> 12));
        buf[1] = (char)(0x80 | ((cp >> 6) & 0x3F));
        buf[2] = (char)(0x80 | (cp & 0x3F));
        n = 3;
    } else {
        buf[0] = (char)(0xF0 | (cp >> 18));
        buf[1] = (char)(0x80 | ((cp >> 12) & 0x3F));
        buf[2] = (char)(0x80 | ((cp >> 6) & 0x3F));
        buf[3] = (char)(0x80 | (cp & 0x3F));
        n = 4;
    }
    return mnd_ptr_v(MND_STR, mnd_str_new(buf, n));
}

static MndValue mnd_bi_ord(MndValue v) {
    if (v.tag != MND_STR) {
        char msg[192];
        snprintf(msg, sizeof(msg), "expected `str` in ord(), found `%s`", mnd_type_name(v));
        mnd_panic(msg);
    }
    MndStr *s = v.u.s;
    if (s->len == 0 || s->len > 4)
        mnd_panic("expected a single-character string in ord()");
    unsigned char c0 = (unsigned char)s->data[0];
    if (s->len == 1) return mnd_int_v(c0);
    int64_t cp = -1;
    if ((c0 & 0xE0) == 0xC0 && s->len == 2)
        cp = ((int64_t)(c0 & 0x1F) << 6) | ((unsigned char)s->data[1] & 0x3F);
    else if ((c0 & 0xF0) == 0xE0 && s->len == 3)
        cp = ((int64_t)(c0 & 0x0F) << 12) | (((unsigned char)s->data[1] & 0x3F) << 6) |
             ((unsigned char)s->data[2] & 0x3F);
    else if ((c0 & 0xF8) == 0xF0 && s->len == 4)
        cp = ((int64_t)(c0 & 0x07) << 18) | (((unsigned char)s->data[1] & 0x3F) << 12) |
             (((unsigned char)s->data[2] & 0x3F) << 6) | ((unsigned char)s->data[3] & 0x3F);
    if (cp < 0) mnd_panic("expected a single-character string in ord()");
    return mnd_int_v(cp);
}

static void mnd_list_push(MndList *l, MndValue v) {
    if (l->len >= l->cap) {
        int64_t nc = l->cap ? l->cap * 2 : 8;
        MndValue *ni = mnd_alloc(sizeof(MndValue) * (size_t)nc);
        for (int64_t i = 0; i < l->len; i++) ni[i] = l->items[i];
        l->items = ni;
        l->cap = nc;
    }
    l->items[l->len++] = v;
}

static MndValue mnd_bi_push(MndValue l, MndValue v) {
    if (l.tag != MND_LIST) {
        char msg[192];
        snprintf(msg, sizeof(msg), "expected a `list`, found `%s`", mnd_type_name(l));
        mnd_panic(msg);
    }
    mnd_list_push(l.u.l, v);
    return l;
}

static MndValue mnd_bi_pop(MndValue l) {
    if (l.tag != MND_LIST) {
        char msg[192];
        snprintf(msg, sizeof(msg), "expected a `list`, found `%s`", mnd_type_name(l));
        mnd_panic(msg);
    }
    if (l.u.l->len == 0) mnd_panic("pop from an empty list");
    return l.u.l->items[--l.u.l->len];
}

static MndValue mnd_bi_split(MndValue s, MndValue sep) {
    MndValue args[1];
    args[0] = sep;
    return mnd_sm_split(s, 1, args);
}

static MndValue mnd_bi_substr(MndValue s, MndValue start, MndValue ln) {
    if (s.tag != MND_STR) {
        char msg[192];
        snprintf(msg, sizeof(msg), "substr() expects a `str`, found `%s`", mnd_type_name(s));
        mnd_panic(msg);
    }
    MndValue args[2];
    args[0] = start;
    args[1] = ln;
    return mnd_sm_substr(s, 2, args);
}

static MndValue mnd_bi_read_file(MndValue p) {
    if (p.tag != MND_STR) mnd_panic("read_file() expects a `str` path");
    FILE *f = fopen(p.u.s->data, "rb");
    if (f == NULL) {
        fprintf(stderr, "error: cannot read file '%s'\n", p.u.s->data);
        exit(1);
    }
    MndBuf b = {NULL, 0, 0};
    char chunk[65536];
    size_t n;
    while ((n = fread(chunk, 1, sizeof(chunk), f)) > 0) mnd_buf_put(&b, chunk, (int64_t)n);
    fclose(f);
    return mnd_ptr_v(MND_STR, mnd_str_new(b.data ? b.data : "", b.len));
}

static MndValue mnd_bi_write_file(MndValue p, MndValue data) {
    if (p.tag != MND_STR || data.tag != MND_STR)
        mnd_panic("write_file() expects two `str` arguments");
    FILE *f = fopen(p.u.s->data, "wb");
    if (f == NULL) {
        fprintf(stderr, "error: cannot write file '%s'\n", p.u.s->data);
        exit(1);
    }
    if (data.u.s->len > 0) fwrite(data.u.s->data, 1, (size_t)data.u.s->len, f);
    fclose(f);
    return mnd_nil_v();
}

static MndValue mnd_bi_args(void) {
    MndList *src = mnd_g_args.u.l;
    MndValue out = mnd_list_lit(0, NULL);
    for (int64_t i = 0; i < src->len; i++) mnd_list_push(out.u.l, src->items[i]);
    return out;
}

static MndValue mnd_bi_fatal(MndValue msg) {
    if (msg.tag != MND_STR) mnd_panic("fatal() expects a `str` message");
    fprintf(stderr, "%s\n", msg.u.s->data);
    exit(1);
    return mnd_nil_v();
}

static MndValue mnd_bi_exit(MndValue code) {
    if (code.tag != MND_INT) mnd_panic("exit() expects an `int` code");
    exit((int)code.u.i);
    return mnd_nil_v();
}

static MndValue mnd_undef_var(const char *name) {
    char msg[192];
    snprintf(msg, sizeof(msg), "undefined variable '%s'", name);
    mnd_panic(msg);
    return mnd_nil_v();
}

static MndValue mnd_range_err(void) {
    mnd_panic("`range` is only allowed in `for` loops");
    return mnd_nil_v();
}

static MndValue mnd_call_err(void) {
    mnd_panic("cannot call a non-function expression");
    return mnd_nil_v();
}

static MndValue mnd_rt_args(int argc, char **argv) {
    MndValue out = mnd_list_lit(0, NULL);
    for (int i = 1; i < argc; i++) mnd_list_push(out.u.l, mnd_str_v(argv[i]));
    return out;
}
'''


class CodeGen:
    def __init__(self):
        self.head = []      # runtime C
        self.protos = []    # prototypes for user functions and methods
        self.tables = []    # struct definition tables
        self.bodies = []    # function and method bodies
        self.gdecls = []    # global variable declarations
        self.main = []      # top-level statements (main body)
        self.tmpn = 0
        self.scopes = [{}]
        self.globals = {}
        self.struct_order = []
        self.struct_fields = {}
        self.struct_methods = {}
        self.fns = []
        self.methods = []   # (struct name, def node)

    # -- naming ------------------------------------------------------

    def newtmp(self):
        self.tmpn += 1
        return "t%d" % self.tmpn

    def newname(self, base):
        self.tmpn += 1
        return "%s_%d" % (base, self.tmpn)

    def push_scope(self):
        self.scopes.append({})

    def pop_scope(self):
        self.scopes.pop()

    def declare(self, name):
        cname = self.newname("v_" + name)
        self.scopes[-1][name] = cname
        return cname

    def lookup(self, name):
        for sc in reversed(self.scopes):
            if name in sc:
                return sc[name]
        if name in self.globals:
            return self.globals[name]
        return None

    # -- helpers -------------------------------------------------------

    def temps(self, exprs):
        pre = []
        names = []
        for e in exprs:
            t = self.newtmp()
            pre.append("MndValue %s = %s;" % (t, e))
            names.append(t)
        return "".join(pre), names

    def calln(self, fn, exprs):
        if not exprs:
            return "%s()" % fn
        if len(exprs) == 1:
            return "%s(%s)" % (fn, exprs[0])
        pre, ns = self.temps(exprs)
        return "({ %s %s(%s); })" % (pre, fn, ", ".join(ns))

    def call_arr(self, fn, exprs):
        if not exprs:
            return "%s(0, NULL)" % fn
        pre, ns = self.temps(exprs)
        return "({ %s %s(%d, (const MndValue[]){%s}); })" % (
            pre, fn, len(exprs), ", ".join(ns))

    # -- collection ------------------------------------------------------

    def collect(self, items):
        for it in items:
            if it[0] == "struct":
                name = it[1]
                self.struct_order.append(name)
                self.struct_fields[name] = [f[0] for f in it[2]]
                self.struct_methods[name] = it[3]
                for m in it[3]:
                    self.methods.append((name, m))
            elif it[0] == "def":
                self.fns.append(it)

    # -- statements --------------------------------------------------------

    def emit_main(self, s):
        self.main.append(s)

    def stmt(self, node, out=None):
        out = self.main if out is None else out
        tag = node[0]
        if tag == "let":
            val = self.expr(node[3])
            if len(self.scopes) == 1 and node[1] in self.globals:
                out.append("%s = %s;" % (self.globals[node[1]], val))
            else:
                cname = self.declare(node[1])
                out.append("MndValue %s = %s;" % (cname, val))
        elif tag == "assign":
            self.assign(node, out)
        elif tag == "expr":
            out.append("(void)(%s);" % self.expr(node[1]))
        elif tag == "return":
            v = self.expr(node[1]) if node[1] is not None else "mnd_nil_v()"
            out.append("return %s;" % v)
        elif tag == "if":
            self.gen_if(node, out)
        elif tag == "while":
            out.append("while (mnd_truthy(%s)) {" % self.expr(node[1]))
            self.push_scope()
            for s in node[2]:
                self.stmt(s, out)
            self.pop_scope()
            out.append("}")
        elif tag == "for":
            self.gen_for(node, out)
        elif tag == "unsafe":
            out.append("{")
            self.push_scope()
            for s in node[1]:
                self.stmt(s, out)
            self.pop_scope()
            out.append("}")
        elif tag == "break":
            out.append("break;")
        elif tag == "continue":
            out.append("continue;")
        else:
            raise mandela.Panic("codegen: unknown statement '%s'" % tag)

    def assign(self, node, out):
        target = node[1]
        if target[0] == "var":
            cname = self.lookup(target[1])
            if cname is None:
                out.append('(void)mnd_undef_var("%s");' % target[1])
                return
            out.append("%s = %s;" % (cname, self.expr(node[2])))
        elif target[0] == "field":
            base = self.expr(target[1])
            val = self.expr(node[2])
            pre, ns = self.temps([base, val])
            name_lit, _ = c_str(target[2])
            out.append("%s mnd_struct_set(%s, %s, %s);" % (pre, ns[0], name_lit, ns[1]))
        else:
            base = self.expr(target[1])
            key = self.expr(target[2])
            val = self.expr(node[2])
            pre, ns = self.temps([base, key, val])
            out.append("%s mnd_index_set(%s, %s, %s);" % (pre, ns[0], ns[1], ns[2]))

    def gen_if(self, node, out):
        first = True
        for cond, body in node[1]:
            out.append("%s (mnd_truthy(%s)) {" %
                       ("if" if first else "else if", self.expr(cond)))
            first = False
            self.push_scope()
            for s in body:
                self.stmt(s, out)
            self.pop_scope()
            out.append("}")
        if node[2] is not None:
            out.append("else {")
            self.push_scope()
            for s in node[2]:
                self.stmt(s, out)
            self.pop_scope()
            out.append("}")

    def gen_for(self, node, out):
        name, it, body = node[1], node[2], node[3]
        if it[0] == "call" and it[1][0] == "var" and it[1][1] == "range":
            args = it[2]
            if len(args) == 1:
                fb = self.newtmp()
                out.append("{")
                out.append("int64_t %s = mnd_as_i64(%s);" % (fb, self.expr(args[0])))
                out.append("for (int64_t fi = 0; fi < %s; fi += 1) {" % fb)
            elif len(args) == 2:
                fa, fb = self.newtmp(), self.newtmp()
                out.append("{")
                out.append("int64_t %s = mnd_as_i64(%s);" % (fa, self.expr(args[0])))
                out.append("int64_t %s = mnd_as_i64(%s);" % (fb, self.expr(args[1])))
                out.append("for (int64_t fi = %s; fi < %s; fi += 1) {" % (fa, fb))
            else:
                fa, fb, fs = self.newtmp(), self.newtmp(), self.newtmp()
                out.append("{")
                out.append("int64_t %s = mnd_as_i64(%s);" % (fa, self.expr(args[0])))
                out.append("int64_t %s = mnd_as_i64(%s);" % (fb, self.expr(args[1])))
                out.append("int64_t %s = mnd_as_i64(%s);" % (fs, self.expr(args[2])))
                out.append('if (%s == 0) mnd_panic("range() arg 3 must not be zero");' % fs)
                out.append("for (int64_t fi = %s; (%s > 0 ? fi < %s : fi > %s); fi += %s) {"
                           % (fa, fs, fb, fb, fs))
            self.push_scope()
            vc = self.declare(name)
            out.append("MndValue %s = mnd_int_v(fi);" % vc)
            for s in body:
                self.stmt(s, out)
            self.pop_scope()
            out.append("}")
            out.append("}")
            return
        fv = self.newtmp()
        out.append("{")
        out.append("MndValue %s = mnd_iter_view(%s);" % (fv, self.expr(it)))
        out.append("int64_t fn = mnd_iter_len(%s);" % fv)
        out.append("for (int64_t fi = 0; fi < fn; fi += 1) {")
        self.push_scope()
        vc = self.declare(name)
        out.append("MndValue %s = mnd_iter_get(%s, fi);" % (vc, fv))
        for s in body:
            self.stmt(s, out)
        self.pop_scope()
        out.append("}")
        out.append("}")

    # -- expressions -------------------------------------------------------

    def expr(self, node):
        tag = node[0]
        if tag == "num":
            n = node[1]
            if n > (1 << 63) - 1:
                n = mandela.wrap64(n)
            if n == -(1 << 63):
                return "mnd_int_v((-9223372036854775807LL) - 1)"
            return "mnd_int_v(%dLL)" % n
        if tag == "str":
            lit, n = c_str(node[1])
            return "mnd_str_lit(%s, %d)" % (lit, n)
        if tag == "bool":
            return "mnd_bool_v(%d)" % (1 if node[1] else 0)
        if tag == "nil":
            return "mnd_nil_v()"
        if tag == "var":
            cname = self.lookup(node[1])
            if cname is None:
                return 'mnd_undef_var("%s")' % node[1]
            return cname
        if tag == "list":
            return self.call_arr("mnd_list_lit", [self.expr(x) for x in node[1]])
        if tag == "dict":
            es = []
            for p in node[1]:
                es.append(self.expr(p[0]))
                es.append(self.expr(p[1]))
            return self.call_arr("mnd_dict_lit", es)
        if tag == "slit":
            es = [self.expr(p[1]) for p in node[2]]
            names = ", ".join(c_str(p[0])[0] for p in node[2])
            if es:
                pre, ns = self.temps(es)
                arr = "(const MndValue[]){%s}" % ", ".join(ns)
                narr = "(const char *const[]){%s}" % names
            else:
                pre = ""
                arr = "NULL"
                narr = "NULL"
            return "({ %s mnd_struct_lit(&mnd_def_%s, %d, %s, %s); })" % (
                pre, node[1], len(es), narr, arr)
        if tag == "field":
            name_lit, _ = c_str(node[2])
            return "mnd_struct_get(%s, %s)" % (self.expr(node[1]), name_lit)
        if tag == "index":
            base = self.expr(node[1])
            key = self.expr(node[2])
            pre, ns = self.temps([base, key])
            return "({ %s mnd_index_get(%s, %s); })" % (pre, ns[0], ns[1])
        if tag == "bin":
            return self.gen_bin(node)
        if tag == "un":
            if node[1] == "-":
                return "mnd_neg(%s)" % self.expr(node[2])
            return "mnd_not(%s)" % self.expr(node[2])
        if tag == "call":
            return self.gen_call(node)
        raise mandela.Panic("codegen: unknown expression '%s'" % tag)

    def gen_bin(self, node):
        op = node[1]
        l = self.expr(node[2])
        r = self.expr(node[3])
        if op == "and":
            t = self.newtmp()
            return ("({ MndValue %s = %s; mnd_bool_v(mnd_truthy(%s) && mnd_truthy(%s)); })"
                    % (t, l, t, r))
        if op == "or":
            t = self.newtmp()
            return ("({ MndValue %s = %s; mnd_bool_v(mnd_truthy(%s) || mnd_truthy(%s)); })"
                    % (t, l, t, r))
        pre, ns = self.temps([l, r])
        a, b = ns
        if op == "==":
            return "({ %s mnd_bool_v(mnd_eq(%s, %s)); })" % (pre, a, b)
        if op == "!=":
            return "({ %s mnd_bool_v(!mnd_eq(%s, %s)); })" % (pre, a, b)
        fn = {"<": "mnd_lt", "<=": "mnd_le", ">": "mnd_gt", ">=": "mnd_ge",
              "+": "mnd_add", "-": "mnd_sub", "*": "mnd_mul", "/": "mnd_div",
              "%": "mnd_rem"}[op]
        return "({ %s %s(%s, %s); })" % (pre, fn, a, b)

    def gen_call(self, node):
        fnode = node[1]
        if fnode[0] == "field":
            recv = self.expr(fnode[1])
            mname = fnode[2]
            pre_r, nr = self.temps([recv])
            es = [self.expr(a) for a in node[2]]
            if es:
                pre_a, na = self.temps(es)
                arr = "(const MndValue[]){%s}" % ", ".join(na)
            else:
                pre_a = ""
                arr = "NULL"
            name_lit, _ = c_str(mname)
            return "({ %s %s mnd_call_method(%s, %s, %d, %s); })" % (
                pre_r, pre_a, nr[0], name_lit, len(es), arr)
        if fnode[0] != "var":
            return "mnd_call_err()"
        name = fnode[1]
        es = [self.expr(a) for a in node[2]]
        builtins = {
            "print": "mnd_bi_print", "eprint": "mnd_bi_eprint", "len": "mnd_bi_len",
            "str": "mnd_bi_str", "int": "mnd_bi_int", "chr": "mnd_bi_chr",
            "ord": "mnd_bi_ord", "push": "mnd_bi_push", "pop": "mnd_bi_pop",
            "split": "mnd_bi_split", "substr": "mnd_bi_substr",
            "read_file": "mnd_bi_read_file", "write_file": "mnd_bi_write_file",
            "args": "mnd_bi_args", "fatal": "mnd_bi_fatal", "exit": "mnd_bi_exit",
            "range": "mnd_range_err",
            "alloc": "mnd_bi_alloc", "arena_new": "mnd_bi_arena_new",
            "arena_alloc": "mnd_bi_arena_alloc", "arena_free": "mnd_bi_arena_free",
            "ptr_len": "mnd_bi_ptr_len", "is_null": "mnd_bi_is_null",
        }
        if name in builtins:
            return self.calln(builtins[name], es)
        return self.calln("mnd_fn_" + name, es)

    # -- functions ------------------------------------------------------------

    def gen_fn(self, f, sname=None):
        name, params, body = f[1], f[2], f[4]
        self.push_scope()
        decls = []
        arith = ""
        if sname is not None:
            want = len(params) - 1
            arith = 'if (n != %d) mnd_panic_arity("%s", %d, n);' % (want, name, want)
            self.declare("self")
            decls.append("MndValue %s = recv;" % self.lookup("self"))
            for i, p in enumerate(params[1:]):
                pc = self.declare(p[0])
                decls.append("MndValue %s = args[%d];" % (pc, i))
            sig = ("static MndValue mnd_m_%s_%s(MndValue recv, int64_t n, "
                   "const MndValue *args)") % (sname, name)
        else:
            psigs = []
            for p in params:
                pc = self.declare(p[0])
                psigs.append("MndValue %s" % pc)
            sig = "static MndValue mnd_fn_%s(%s)" % (name, ", ".join(psigs) or "void")
        self.bodies.append(sig + " {")
        if arith:
            self.bodies.append("    " + arith)
        for d in decls:
            self.bodies.append("    " + d)
        for s in body:
            self.stmt(s, self.bodies)
        self.bodies.append("    return mnd_nil_v();")
        self.bodies.append("}")
        self.pop_scope()

    # -- top level --------------------------------------------------------------

    def generate(self, items):
        self.collect(items)
        for it in items:
            if it[0] == "let" and it[1] not in self.globals:
                self.globals[it[1]] = "g_" + it[1]
                self.gdecls.append("static MndValue %s;" % self.globals[it[1]])
        for sname, m in self.methods:
            self.protos.append(
                "static MndValue mnd_m_%s_%s(MndValue, int64_t, const MndValue *);"
                % (sname, m[1]))
        for f in self.fns:
            self.protos.append("static MndValue mnd_fn_%s(%s);" % (
                f[1], ", ".join(["MndValue"] * len(f[2])) or "void"))
        for sname in self.struct_order:
            fields = self.struct_fields[sname]
            methods = self.struct_methods[sname]
            farr = ", ".join(c_str(f)[0] for f in fields) or '""'
            marr = ", ".join('{"%s", mnd_m_%s_%s}' % (m[1], sname, m[1])
                             for m in methods) or '{0, 0}'
            self.tables.append("static const char *mnd_fld_%s[] = {%s};"
                               % (sname, farr))
            self.tables.append("static const MndMethodEntry mnd_mtab_%s[] = {%s};"
                               % (sname, marr))
            self.tables.append(
                'static const MndStructDef mnd_def_%s = {"%s", %d, mnd_fld_%s, %d, mnd_mtab_%s};'
                % (sname, sname, len(fields), sname, len(methods), sname))
        for sname, m in self.methods:
            self.gen_fn(m, sname=sname)
        for f in self.fns:
            self.gen_fn(f)
        for it in items:
            if it[0] in ("def", "struct"):
                continue
            self.stmt(it)
        parts = [RUNTIME_C, ""]
        parts.append("/* ---- generated program ---- */")
        parts.extend(self.protos)
        parts.extend(self.tables)
        parts.append("")
        parts.extend(self.gdecls)
        parts.extend(self.bodies)
        parts.append("")
        parts.append("int main(int argc, char **argv) {")
        parts.append("    mnd_g_args = mnd_rt_args(argc, argv);")
        for line in self.main:
            parts.append("    " + line)
        parts.append("    return 0;")
        parts.append("}")
        return "\n".join(parts) + "\n"


def fail(msg, code=1):
    sys.stderr.write(msg.rstrip("\n") + "\n")
    sys.exit(code)


def usage():
    sys.stderr.write(
        "usage: mandela build <file.mnd> [-o out] [--emit c|asm|bin]\n"
        "  -o out      output base name (default: file name without .mnd)\n"
        "  --emit c    write the generated C source (out.c)\n"
        "  --emit asm  write assembly produced by the C toolchain (out.s)\n"
        "  --emit bin  build a native executable (default)\n")
    sys.exit(2)


def main():
    argv = sys.argv[1:]
    src = None
    out = None
    emit = "bin"
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "-o" and i + 1 < len(argv):
            out = argv[i + 1]
            i += 2
        elif a == "--emit" and i + 1 < len(argv):
            emit = argv[i + 1]
            i += 2
        elif a.startswith("--emit="):
            emit = a[len("--emit="):]
            i += 1
        elif a.startswith("-o") and len(a) > 2:
            out = a[2:]
            i += 1
        elif a in ("-h", "--help"):
            usage()
        elif src is None:
            src = a
            i += 1
        else:
            sys.stderr.write("error: unexpected argument '%s'\n" % a)
            usage()
    if src is None:
        usage()
    if emit not in ("c", "asm", "bin"):
        fail("error: unknown --emit value '%s' (expected c, asm or bin)" % emit)
    base = out
    if base is None:
        base = src[:-4] if src.endswith(".mnd") else src
    try:
        with open(src, encoding="utf-8") as fh:
            text = fh.read()
    except (OSError, UnicodeDecodeError):
        fail("error: cannot open file '%s'" % src)
    try:
        toks = mandela.tokenize(text)
        ast = mandela.Parser(toks).parse()
    except mandela.CompileError as e:
        fail("error: %s\n  --> %s:%d:%d" % (e.msg, src, e.line, e.col))
    cg = CodeGen()
    try:
        csource = cg.generate(ast[1])
    except mandela.Panic as e:
        fail("error: %s" % e.msg)

    if emit == "c":
        path = base if base.endswith(".c") else base + ".c"
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(csource)
        return

    cc = os.environ.get("CC", "cc")
    if shutil.which(cc) is None:
        fail("error: cannot find a C compiler (`%s`); set $CC" % cc)
    fd, tmppath = tempfile.mkstemp(suffix=".c", prefix=".mandela")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(csource)
    try:
        if emit == "asm":
            path = base if base.endswith(".s") else base + ".s"
            cmd = [cc, "-O2", "-S", "-o", path, tmppath]
        else:
            path = base
            cmd = [cc, "-O2", "-o", path, tmppath]
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if proc.returncode != 0:
            sys.stderr.write(proc.stderr.decode("utf-8", "replace"))
            fail("error: C compiler failed while building '%s'" % path)
    finally:
        try:
            os.remove(tmppath)
        except OSError:
            pass


if __name__ == "__main__":
    main()
