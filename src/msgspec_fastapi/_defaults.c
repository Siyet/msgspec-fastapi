#include <Python.h>
#include <stdint.h>

#define GUARD_POLICY_FIELDS(X)                                                                     \
    X(fallback)                                                                                    \
    X(registry)                                                                                    \
    X(trusted_union_reducer)                                                                       \
    X(union_type)                                                                                  \
    X(function_type)                                                                               \
    X(builtin_function_type)                                                                       \
    X(getset_descriptor_type)                                                                      \
    X(member_descriptor_type)                                                                      \
    X(method_descriptor_type)                                                                      \
    X(wrapper_descriptor_type)                                                                     \
    X(struct_type)                                                                                 \
    X(enum_type)                                                                                   \
    X(atomic_ids)                                                                                  \
    X(reference_ids)                                                                               \
    X(model_brands)                                                                                \
    X(native_state_methods)                                                                        \
    X(state_names)                                                                                 \
    X(enum_namespace)                                                                              \
    X(missing)                                                                                     \
    X(mro_descriptor)                                                                              \
    X(dict_descriptor)                                                                             \
    X(name_descriptor)

#define GUARD_NAME_FIELDS(X)                                                                       \
    X(copy_name)                                                                                   \
    X(name_name)                                                                                   \
    X(deepcopy_name)                                                                               \
    X(dict_name)                                                                                   \
    X(dataclass_fields_name)                                                                       \
    X(kind_atomic)                                                                                 \
    X(kind_state)                                                                                  \
    X(slots_name)                                                                                  \
    X(slotnames_name)

typedef struct {
    PyObject_HEAD
#define GUARD_REF(name) PyObject *name;
    GUARD_POLICY_FIELDS(GUARD_REF)
    GUARD_NAME_FIELDS(GUARD_REF)
#undef GUARD_REF
    PyObject *object_namespace;
    descrgetfunc get_mro;
    descrgetfunc get_dict;
    descrgetfunc get_name;
    int initialized;
} Guard;

static int guard_traverse(Guard *self, visitproc visit, void *arg) {
    Py_VISIT(Py_TYPE((PyObject *)self));
    Py_VISIT(self->object_namespace);
#define GUARD_VISIT(name) Py_VISIT(self->name);
    GUARD_POLICY_FIELDS(GUARD_VISIT)
    GUARD_NAME_FIELDS(GUARD_VISIT)
#undef GUARD_VISIT
    return 0;
}

static int guard_clear(Guard *self) {
    Py_CLEAR(self->object_namespace);
#define GUARD_CLEAR(name) Py_CLEAR(self->name);
    GUARD_POLICY_FIELDS(GUARD_CLEAR)
    GUARD_NAME_FIELDS(GUARD_CLEAR)
#undef GUARD_CLEAR
    self->get_mro = NULL;
    self->get_dict = NULL;
    self->get_name = NULL;
    return 0;
}

static void guard_dealloc(Guard *self) {
    PyTypeObject *type = Py_TYPE((PyObject *)self);
    PyObject_GC_UnTrack(self);
    guard_clear(self);
    PyObject_GC_Del(self);
    Py_DECREF((PyObject *)type);
}

static int guard_ready(Guard *self) {
    if (!self->initialized || self->fallback == NULL) {
        PyErr_SetString(PyExc_TypeError, "Uninitialized _Guard");
        return 0;
    }
    return 1;
}

static int string_tuple(PyObject *value) {
    if (!PyTuple_CheckExact(value))
        return 0;
    Py_ssize_t size = PyTuple_Size(value);
    for (Py_ssize_t i = 0; i < size; i++) {
        if (!PyUnicode_CheckExact(PyTuple_GetItem(value, i)))
            return 0;
    }
    return 1;
}

static int id_collection(PyObject *value) {
    if (!PySet_CheckExact(value) && !PyFrozenSet_CheckExact(value))
        return 0;
    PyObject *iter = PyObject_GetIter(value);
    if (iter == NULL)
        return -1;
    PyObject *item;
    while ((item = PyIter_Next(iter)) != NULL) {
        int valid = PyLong_CheckExact(item);
        Py_DECREF(item);
        if (!valid) {
            Py_DECREF(iter);
            return 0;
        }
    }
    Py_DECREF(iter);
    return PyErr_Occurred() ? -1 : 1;
}

static int guard_init(Guard *self, PyObject *args, PyObject *kwargs) {
    if (self->initialized) {
        PyErr_SetString(PyExc_TypeError, "_Guard cannot be reinitialized");
        return -1;
    }
#define GUARD_PARAM(name) PyObject *name;
    GUARD_POLICY_FIELDS(GUARD_PARAM)
#undef GUARD_PARAM
#define GUARD_KEY(name) #name,
    static char *keywords[] = {GUARD_POLICY_FIELDS(GUARD_KEY) NULL};
#undef GUARD_KEY
    if (!PyArg_ParseTupleAndKeywords(
            args, kwargs, "OOOOOOOOOOOOOOOOOOOOOO:_Guard", keywords, &fallback, &registry,
            &trusted_union_reducer, &union_type, &function_type, &builtin_function_type,
            &getset_descriptor_type, &member_descriptor_type, &method_descriptor_type,
            &wrapper_descriptor_type, &struct_type, &enum_type, &atomic_ids, &reference_ids,
            &model_brands, &native_state_methods, &state_names, &enum_namespace, &missing,
            &mro_descriptor, &dict_descriptor, &name_descriptor))
        return -1;
    if (!PyCallable_Check(fallback)) {
        PyErr_SetString(PyExc_TypeError, "fallback must be callable");
        return -1;
    }
    if (!PyDict_CheckExact(registry)) {
        PyErr_SetString(PyExc_TypeError, "registry must be an exact dict");
        return -1;
    }
    if (!PyType_Check(union_type)) {
        PyErr_SetString(PyExc_TypeError, "union_type must be a type");
        return -1;
    }
    if (!PyType_Check(function_type)) {
        PyErr_SetString(PyExc_TypeError, "function_type must be a type");
        return -1;
    }
    if (!PyType_Check(builtin_function_type)) {
        PyErr_SetString(PyExc_TypeError, "builtin_function_type must be a type");
        return -1;
    }
    if (!PyType_Check(getset_descriptor_type)) {
        PyErr_SetString(PyExc_TypeError, "getset_descriptor_type must be a type");
        return -1;
    }
    if (!PyType_Check(member_descriptor_type)) {
        PyErr_SetString(PyExc_TypeError, "member_descriptor_type must be a type");
        return -1;
    }
    if (!PyType_Check(method_descriptor_type)) {
        PyErr_SetString(PyExc_TypeError, "method_descriptor_type must be a type");
        return -1;
    }
    if (!PyType_Check(wrapper_descriptor_type)) {
        PyErr_SetString(PyExc_TypeError, "wrapper_descriptor_type must be a type");
        return -1;
    }
    if (!PyType_Check(struct_type)) {
        PyErr_SetString(PyExc_TypeError, "struct_type must be a type");
        return -1;
    }
    if (!PyType_Check(enum_type)) {
        PyErr_SetString(PyExc_TypeError, "enum_type must be a type");
        return -1;
    }
    int atomic_valid = id_collection(atomic_ids);
    if (atomic_valid < 0)
        return -1;
    int reference_valid = id_collection(reference_ids);
    if (reference_valid < 0)
        return -1;
    if (!atomic_valid || !reference_valid) {
        PyErr_SetString(PyExc_TypeError, "Policy ID collections must contain exact integer IDs");
        return -1;
    }
    if (!string_tuple(model_brands) || !string_tuple(state_names)) {
        PyErr_SetString(PyExc_TypeError, "Policy names must be tuples of exact strings");
        return -1;
    }
    if (!PyTuple_CheckExact(native_state_methods)) {
        PyErr_SetString(PyExc_TypeError, "Invalid native state policy");
        return -1;
    }
    for (Py_ssize_t i = 0; i < PyTuple_Size(native_state_methods); i++) {
        PyObject *pair = PyTuple_GetItem(native_state_methods, i);
        if (!PyTuple_CheckExact(pair) || PyTuple_Size(pair) != 2 ||
            !PyUnicode_CheckExact(PyTuple_GetItem(pair, 0)) ||
            !PyTuple_CheckExact(PyTuple_GetItem(pair, 1))) {
            PyErr_SetString(PyExc_TypeError, "Invalid native state policy");
            return -1;
        }
    }
    if ((PyObject *)Py_TYPE(mro_descriptor) != getset_descriptor_type &&
        (PyObject *)Py_TYPE(mro_descriptor) != member_descriptor_type) {
        PyErr_SetString(PyExc_TypeError, "mro_descriptor must be a native type descriptor");
        return -1;
    }
    if ((PyObject *)Py_TYPE(dict_descriptor) != getset_descriptor_type ||
        (PyObject *)Py_TYPE(enum_namespace) != getset_descriptor_type) {
        PyErr_SetString(PyExc_TypeError,
                        "Policy namespace descriptors must be native getset descriptors");
        return -1;
    }
    if ((PyObject *)Py_TYPE(name_descriptor) != getset_descriptor_type &&
        (PyObject *)Py_TYPE(name_descriptor) != member_descriptor_type) {
        PyErr_SetString(PyExc_TypeError, "name_descriptor must be a native type descriptor");
        return -1;
    }
    descrgetfunc get_name = (descrgetfunc)PyType_GetSlot(Py_TYPE(name_descriptor), Py_tp_descr_get);
    descrgetfunc get_mro = (descrgetfunc)PyType_GetSlot(Py_TYPE(mro_descriptor), Py_tp_descr_get);
    descrgetfunc get_dict = (descrgetfunc)PyType_GetSlot(Py_TYPE(dict_descriptor), Py_tp_descr_get);
    if (!get_mro || !get_dict || !get_name) {
        if (!PyErr_Occurred())
            PyErr_SetString(PyExc_TypeError, "Invalid native type descriptors");
        return -1;
    }
#define GUARD_STORE(name) self->name = Py_NewRef(name);
    GUARD_POLICY_FIELDS(GUARD_STORE)
#undef GUARD_STORE
    self->copy_name = PyUnicode_InternFromString("copy");
    if (!self->copy_name) {
        guard_clear(self);
        return -1;
    }
    self->name_name = PyUnicode_InternFromString("__name__");
    if (!self->name_name) {
        guard_clear(self);
        return -1;
    }
    self->deepcopy_name = PyUnicode_InternFromString("__deepcopy__");
    if (!self->deepcopy_name) {
        guard_clear(self);
        return -1;
    }
    self->dict_name = PyUnicode_InternFromString("__dict__");
    if (!self->dict_name) {
        guard_clear(self);
        return -1;
    }
    self->dataclass_fields_name = PyUnicode_InternFromString("__dataclass_fields__");
    if (!self->dataclass_fields_name) {
        guard_clear(self);
        return -1;
    }
    self->kind_atomic = PyUnicode_InternFromString("atomic");
    if (!self->kind_atomic) {
        guard_clear(self);
        return -1;
    }
    self->kind_state = PyUnicode_InternFromString("state");
    if (!self->kind_state) {
        guard_clear(self);
        return -1;
    }
    self->slots_name = PyUnicode_InternFromString("__slots__");
    if (!self->slots_name) {
        guard_clear(self);
        return -1;
    }
    self->slotnames_name = PyUnicode_InternFromString("__slotnames__");
    if (!self->slotnames_name) {
        guard_clear(self);
        return -1;
    }
    self->get_mro = get_mro;
    self->get_dict = get_dict;
    self->get_name = get_name;
    self->initialized = 1;
    return 0;
}

static int class_id_contains(PyObject *ids, PyTypeObject *cls) {
    PyObject *key = PyLong_FromVoidPtr(cls);
    if (key == NULL)
        return -1;
    int result = PySet_Contains(ids, key);
    Py_DECREF(key);
    return result;
}

static PyObject *class_info_impl(Guard *self, PyObject *cls);

typedef struct {
    PyObject **slots;
    Py_ssize_t cap, count;
} Identity;
static size_t ihash(PyObject *x) {
    return ((uintptr_t)x >> 4) * (size_t)11400714819323198485ull;
}
static int seen_add(Identity *s, PyObject *x) {
    if (!s->cap || s->count >= s->cap - s->cap / 3) {
        if (s->cap > PY_SSIZE_T_MAX / 2) {
            PyErr_NoMemory();
            return -1;
        }
        Py_ssize_t cap = s->cap ? s->cap * 2 : 32;
        if ((size_t)cap > SIZE_MAX / sizeof(PyObject *)) {
            PyErr_NoMemory();
            return -1;
        }
        PyObject **out = PyMem_Calloc((size_t)cap, sizeof(PyObject *));
        if (!out) {
            PyErr_NoMemory();
            return -1;
        }
        for (Py_ssize_t i = 0; i < s->cap; i++)
            if (s->slots[i]) {
                size_t k = ihash(s->slots[i]) & (cap - 1);
                while (out[k])
                    k = (k + 1) & (cap - 1);
                out[k] = s->slots[i];
            }
        PyMem_Free(s->slots);
        s->slots = out;
        s->cap = cap;
    }
    size_t k = ihash(x) & (s->cap - 1);
    while (s->slots[k]) {
        if (s->slots[k] == x)
            return 0;
        k = (k + 1) & (s->cap - 1);
    }
    Py_INCREF(x);
    s->slots[k] = x;
    s->count++;
    return 1;
}
static void seen_clear(Identity *s) {
    for (Py_ssize_t i = 0; i < s->cap; i++)
        Py_XDECREF(s->slots[i]);
    PyMem_Free(s->slots);
}
typedef struct {
    PyTypeObject *cls;
    PyObject *info;
    descrgetfunc nsget, slotget;
    int reference;
} Entry;
typedef struct {
    Entry *items;
    Py_ssize_t size, cap;
} Cache;
static Entry *cache_get(Cache *cache, PyTypeObject *cls, Guard *self) {
    for (Py_ssize_t i = 0; i < cache->size; i++)
        if (cache->items[i].cls == cls)
            return &cache->items[i];
    int reference = class_id_contains(self->reference_ids, cls);
    if (reference < 0)
        return NULL;
    int atomic = class_id_contains(self->atomic_ids, cls);
    if (atomic < 0)
        return NULL;
    PyObject *info =
        (reference || atomic) ? Py_NewRef(Py_None) : class_info_impl(self, (PyObject *)cls);
    if (!info)
        return NULL;
    descrgetfunc nsget = NULL, slotget = NULL;
    if (info != Py_None) {
        if (!PyTuple_CheckExact(info) || PyTuple_Size(info) != 3 ||
            !PyTuple_CheckExact(PyTuple_GetItem(info, 2))) {
            Py_DECREF(info);
            PyErr_SetString(PyExc_TypeError, "Invalid class policy result");
            return NULL;
        }
        PyObject *ns = PyTuple_GetItem(info, 1), *slots = PyTuple_GetItem(info, 2);
        if (ns != Py_None)
            nsget = (descrgetfunc)PyType_GetSlot(Py_TYPE(ns), Py_tp_descr_get);
        if (PyTuple_Size(slots)) {
            PyTypeObject *first = Py_TYPE(PyTuple_GetItem(slots, 0));
            slotget = (descrgetfunc)PyType_GetSlot(first, Py_tp_descr_get);
            for (Py_ssize_t i = 1; i < PyTuple_Size(slots); i++)
                if (Py_TYPE(PyTuple_GetItem(slots, i)) != first) {
                    Py_DECREF(info);
                    PyErr_SetString(PyExc_TypeError, "Slot descriptors must have one native type");
                    return NULL;
                }
        }
        if ((ns != Py_None && !nsget) || (PyTuple_Size(slots) && !slotget)) {
            Py_DECREF(info);
            if (!PyErr_Occurred())
                PyErr_SetString(PyExc_TypeError, "Invalid state descriptor");
            return NULL;
        }
    }
    if (cache->size == cache->cap) {
        if (cache->cap > PY_SSIZE_T_MAX / 2) {
            Py_DECREF(info);
            PyErr_NoMemory();
            return NULL;
        }
        Py_ssize_t cap = cache->cap ? cache->cap * 2 : 4;
        if ((size_t)cap > SIZE_MAX / sizeof(Entry)) {
            Py_DECREF(info);
            PyErr_NoMemory();
            return NULL;
        }
        Entry *out = PyMem_Realloc(cache->items, (size_t)cap * sizeof(Entry));
        if (!out) {
            Py_DECREF(info);
            PyErr_NoMemory();
            return NULL;
        }
        cache->items = out;
        cache->cap = cap;
    }
    Py_INCREF((PyObject *)cls);
    Entry *entry = &cache->items[cache->size++];
    entry->cls = cls;
    entry->info = info;
    entry->nsget = nsget;
    entry->slotget = slotget;
    entry->reference = reference;
    return entry;
}
static void cache_clear(Cache *cache) {
    for (Py_ssize_t i = 0; i < cache->size; i++) {
        Py_DECREF(cache->items[i].cls);
        Py_DECREF(cache->items[i].info);
    }
    PyMem_Free(cache->items);
}
static int atom(PyObject *x) {
    return x == Py_None || PyBool_Check(x) || PyLong_CheckExact(x) || PyFloat_CheckExact(x) ||
           PyUnicode_CheckExact(x) || PyBytes_CheckExact(x);
}
static int push(PyObject *stack, PyObject *x) {
    return atom(x) ? 0 : PyList_Append(stack, x);
}
static int check_slot_binding(Guard *self, PyObject *maps, PyObject *name) {
    for (Py_ssize_t i = 0; i < PyTuple_Size(maps); i++) {
        PyObject *descriptor = PyDict_GetItemWithError(PyTuple_GetItem(maps, i), name);
        if (descriptor != NULL) {
            if ((PyObject *)Py_TYPE(descriptor) == self->member_descriptor_type)
                return 0;
            break;
        }
        if (PyErr_Occurred())
            return -1;
    }
    PyErr_SetString(PyExc_TypeError, "Non-native slot bindings are not supported in copied data");
    return -1;
}

static int declared_slot(Guard *self, PyObject *base, PyObject *maps, PyObject *name) {
    if (!PyUnicode_CheckExact(name)) {
        PyErr_SetString(PyExc_TypeError, "Slot names must be exact strings in copied data");
        return -1;
    }
    if (PyUnicode_CompareWithASCIIString(name, "__dict__") == 0 ||
        PyUnicode_CompareWithASCIIString(name, "__weakref__") == 0)
        return 0;
    Py_ssize_t length = PyUnicode_GetLength(name);
    if (length < 0)
        return -1;
    int private_name = length >= 2 && PyUnicode_ReadChar(name, 0) == '_' &&
                       PyUnicode_ReadChar(name, 1) == '_' &&
                       !(PyUnicode_ReadChar(name, length - 2) == '_' &&
                         PyUnicode_ReadChar(name, length - 1) == '_');
    if (PyErr_Occurred())
        return -1;
    if (!private_name)
        return check_slot_binding(self, maps, name);
    PyObject *class_name = self->get_name(self->name_descriptor, base, (PyObject *)Py_TYPE(base));
    if (class_name == NULL)
        return -1;
    if (!PyUnicode_Check(class_name)) {
        Py_DECREF(class_name);
        PyErr_SetString(PyExc_TypeError, "Invalid native class name descriptor");
        return -1;
    }
    Py_ssize_t class_length = PyUnicode_GetLength(class_name), start = 0;
    if (class_length < 0) {
        Py_DECREF(class_name);
        return -1;
    }
    while (start < class_length && PyUnicode_ReadChar(class_name, start) == '_')
        start++;
    if (PyErr_Occurred()) {
        Py_DECREF(class_name);
        return -1;
    }
    if (start == class_length) {
        Py_DECREF(class_name);
        return check_slot_binding(self, maps, name);
    }
    PyObject *stripped = PyUnicode_Substring(class_name, start, class_length);
    Py_DECREF(class_name);
    if (stripped == NULL)
        return -1;
    PyObject *mangled = PyUnicode_FromFormat("_%U%U", stripped, name);
    Py_DECREF(stripped);
    if (mangled == NULL)
        return -1;
    int status = check_slot_binding(self, maps, mangled);
    Py_DECREF(mangled);
    return status;
}

static int check_slot_bindings(Guard *self, PyObject *mro, PyObject *maps) {
    for (Py_ssize_t i = 0; i < PyTuple_Size(maps); i++) {
        PyObject *base = PyTuple_GetItem(mro, i), *mapping = PyTuple_GetItem(maps, i);
        PyObject *declared = PyDict_GetItemWithError(mapping, self->slots_name);
        if (declared == NULL && PyErr_Occurred())
            return -1;
        if (declared != NULL) {
            if (PyUnicode_CheckExact(declared)) {
                if (declared_slot(self, base, maps, declared) < 0)
                    return -1;
            } else {
                if (!PyTuple_CheckExact(declared) && !PyList_CheckExact(declared) &&
                    !PySet_CheckExact(declared) && !PyFrozenSet_CheckExact(declared) &&
                    !PyDict_CheckExact(declared)) {
                    PyErr_SetString(PyExc_TypeError,
                                    "Unsupported slot declarations in copied data");
                    return -1;
                }
                PyObject *iter = PyObject_GetIter(declared);
                if (iter == NULL)
                    return -1;
                PyObject *name;
                while ((name = PyIter_Next(iter)) != NULL) {
                    int status = declared_slot(self, base, maps, name);
                    Py_DECREF(name);
                    if (status < 0) {
                        Py_DECREF(iter);
                        return -1;
                    }
                }
                Py_DECREF(iter);
                if (PyErr_Occurred())
                    return -1;
            }
        }
        PyObject *cached = PyDict_GetItemWithError(mapping, self->slotnames_name);
        if (cached == NULL && PyErr_Occurred())
            return -1;
        if (cached == NULL || cached == Py_None)
            continue;
        if (!PyList_CheckExact(cached)) {
            PyErr_SetString(PyExc_TypeError, "Unsupported cached slot names in copied data");
            return -1;
        }
        for (Py_ssize_t j = 0; j < PyList_Size(cached); j++) {
            PyObject *name = PyList_GetItem(cached, j);
            if (!PyUnicode_CheckExact(name)) {
                PyErr_SetString(PyExc_TypeError, "Slot names must be exact strings in copied data");
                return -1;
            }
            if (check_slot_binding(self, maps, name) < 0)
                return -1;
        }
    }
    return 0;
}

static PyObject *class_info_impl(Guard *self, PyObject *cls) {
    PyObject *mro = NULL, *maps = NULL, *slots = NULL, *result = NULL;
    if (!PyType_Check(cls)) {
        PyErr_SetString(PyExc_TypeError, "class_info() requires a type");
        return NULL;
    }
    PyObject *brands = self->model_brands;
    PyObject *states = self->native_state_methods;
    PyObject *atomic = self->atomic_ids;
    PyObject *Struct = self->struct_type;
    PyObject *Enum = self->enum_type;
    PyObject *getsettype = self->getset_descriptor_type;
    PyObject *membertype = self->member_descriptor_type;
    PyObject *missing = self->missing;
    mro = self->get_mro(self->mro_descriptor, cls, (PyObject *)Py_TYPE(cls));
    if (!mro)
        goto done;
    if (!PyTuple_CheckExact(mro)) {
        PyErr_SetString(PyExc_TypeError, "Invalid class MRO descriptor");
        goto done;
    }
    maps = PyTuple_New(PyTuple_Size(mro));
    if (!maps)
        goto done;
    for (Py_ssize_t i = 0; i < PyTuple_Size(mro); i++) {
        PyObject *base = PyTuple_GetItem(mro, i), *mapping;
        if (base == (PyObject *)&PyBaseObject_Type && self->object_namespace != NULL) {
            mapping = Py_NewRef(self->object_namespace);
        } else {
            PyObject *proxy = self->get_dict(self->dict_descriptor, base, (PyObject *)Py_TYPE(base));
            if (proxy == NULL)
                goto done;
            mapping = PyObject_CallMethodObjArgs(proxy, self->copy_name, NULL);
            Py_DECREF(proxy);
            if (mapping == NULL)
                goto done;
            if (base == (PyObject *)&PyBaseObject_Type && PyDict_CheckExact(mapping) &&
                self->object_namespace == NULL)
                self->object_namespace = Py_NewRef(mapping);
        }
        if (!PyDict_CheckExact(mapping)) {
            Py_DECREF(mapping);
            PyErr_SetString(PyExc_TypeError, "Invalid class namespace descriptor");
            goto done;
        }
        if (PyTuple_SetItem(maps, i, mapping) < 0)
            goto done;
        for (Py_ssize_t j = 0; j < PyTuple_Size(brands); j++) {
            int has = PyDict_Contains(mapping, PyTuple_GetItem(brands, j));
            if (has) {
                if (has > 0)
                    PyErr_SetString(PyExc_TypeError,
                                    "Pydantic model behavior is not supported for msgspec data");
                goto done;
            }
        }
    }
    int isenum = PyType_IsSubtype((PyTypeObject *)cls, (PyTypeObject *)Enum), isatomic = 0;
    if (isenum < 0)
        goto done;
    if (!isenum) {
        PyObject *bases[5] = {(PyObject *)&PyDict_Type, (PyObject *)&PyList_Type,
                              (PyObject *)&PyTuple_Type, (PyObject *)&PySet_Type,
                              (PyObject *)&PyFrozenSet_Type};
        for (int i = 0; i < 5; i++) {
            int issub = PyType_IsSubtype((PyTypeObject *)cls, (PyTypeObject *)bases[i]);
            if (issub) {
                if (issub > 0)
                    PyErr_SetString(PyExc_TypeError,
                                    "Container subclasses are not supported in copied data");
                goto done;
            }
        }
        PyObject *key = PyLong_FromVoidPtr(cls);
        if (!key)
            goto done;
        isatomic = PySet_Contains(atomic, key);
        Py_DECREF(key);
        if (isatomic < 0)
            goto done;
        if (!isatomic) {
            int supported = PyType_IsSubtype((PyTypeObject *)cls, (PyTypeObject *)Struct);
            if (supported < 0)
                goto done;
            if (!supported) {
                for (Py_ssize_t i = 0; i < PyTuple_Size(maps); i++) {
                    PyObject *fields = PyDict_GetItemWithError(PyTuple_GetItem(maps, i),
                                                               self->dataclass_fields_name);
                    if (fields != NULL) {
                        supported = 1;
                        break;
                    }
                    if (PyErr_Occurred())
                        goto done;
                }
            }
            if (!supported) {
                PyErr_SetString(PyExc_TypeError, "Unsupported object in copied data");
                goto done;
            }
        }
    }
    if (isenum || !isatomic) {
        for (Py_ssize_t i = 0; i < PyTuple_Size(states); i++) {
            PyObject *pair = PyTuple_GetItem(states, i), *name = PyTuple_GetItem(pair, 0),
                     *native = PyTuple_GetItem(pair, 1), *current = missing;
            for (Py_ssize_t j = 0; j < PyTuple_Size(maps); j++) {
                PyObject *v = PyDict_GetItemWithError(PyTuple_GetItem(maps, j), name);
                if (v && v != missing) {
                    current = v;
                    break;
                }
                if (PyErr_Occurred())
                    goto done;
            }
            int allowed = 0;
            for (Py_ssize_t j = 0; j < PyTuple_Size(native); j++)
                if (current == PyTuple_GetItem(native, j)) {
                    allowed = 1;
                    break;
                }
            if (!allowed) {
                PyErr_SetString(
                    PyExc_TypeError,
                    "Custom state or reduction methods are not supported in copied data");
                goto done;
            }
        }
        PyObject *method = missing;
        for (Py_ssize_t j = 0; j < PyTuple_Size(maps); j++) {
            PyObject *v = PyDict_GetItemWithError(PyTuple_GetItem(maps, j), self->deepcopy_name);
            if (v && v != missing) {
                method = v;
                break;
            }
            if (PyErr_Occurred())
                goto done;
        }
        if (method != missing) {
            PyObject *copy_types[3] = {self->function_type, self->method_descriptor_type,
                                       self->wrapper_descriptor_type};
            int allowed = 0;
            for (Py_ssize_t j = 0; j < 3; j++)
                if ((PyObject *)Py_TYPE(method) == copy_types[j]) {
                    allowed = 1;
                    break;
                }
            if (!allowed) {
                PyErr_SetString(PyExc_TypeError,
                                "Custom copy descriptors are not supported in copied data");
                goto done;
            }
        }
    }
    PyObject *namespace = isenum ? self->enum_namespace : Py_None;
    if (!isenum) {
        /* Trusted classes can shadow a base slot with a property, as pathlib does on
           Python 3.13; their slot values are still checked. */
        if (!isatomic && check_slot_bindings(self, mro, maps) < 0)
            goto done;
        for (Py_ssize_t i = 0; i < PyTuple_Size(maps); i++) {
            PyObject *mapping = PyTuple_GetItem(maps, i),
                     *descriptor = PyDict_GetItemWithError(mapping, self->dict_name);
            if (descriptor == NULL && PyErr_Occurred())
                goto done;
            if (descriptor && descriptor != Py_None && namespace == Py_None) {
                if ((PyObject *)Py_TYPE(descriptor) != getsettype) {
                    PyErr_SetString(PyExc_TypeError, "Unsupported instance state in copied data");
                    goto done;
                }
                namespace = descriptor;
            }
            Py_ssize_t pos = 0;
            PyObject *k, *v;
            while (PyDict_Next(mapping, &pos, &k, &v)) {
                if ((PyObject *)Py_TYPE(v) != membertype)
                    continue;
                if (slots == NULL) {
                    slots = PyList_New(0);
                    if (slots == NULL)
                        goto done;
                }
                if (PyList_Append(slots, v) < 0)
                    goto done;
            }
        }
        for (Py_ssize_t i = 0; slots != NULL && i < PyList_Size(slots); i++) {
            PyObject *descriptor = PyList_GetItem(slots, i),
                     *name = PyObject_GetAttr(descriptor, self->name_name), *effective = NULL;
            if (!name)
                goto done;
            for (Py_ssize_t j = 0; j < PyTuple_Size(maps); j++) {
                effective = PyDict_GetItemWithError(PyTuple_GetItem(maps, j), name);
                if (effective || PyErr_Occurred())
                    break;
            }
            Py_DECREF(name);
            if (PyErr_Occurred())
                goto done;
            if (effective != descriptor && !isatomic) {
                PyErr_SetString(PyExc_TypeError, "Shadowed slots are not supported in copied data");
                goto done;
            }
        }
    }
    PyObject *slot_tuple = slots == NULL ? PyTuple_New(0) : PyList_AsTuple(slots);
    if (slot_tuple) {
        result =
            PyTuple_Pack(3, isenum ? self->kind_atomic : self->kind_state, namespace, slot_tuple);
        Py_DECREF(slot_tuple);
    }
done:
    Py_XDECREF(mro);
    Py_XDECREF(maps);
    Py_XDECREF(slots);
    return result;
}
static int registry_snapshot(Guard *self, PyObject **out) {
    PyObject *keys = PyList_New(0);
    if (keys == NULL)
        return -1;
    Py_ssize_t position = 0;
    PyObject *key, *reducer;
    while (PyDict_Next(self->registry, &position, &key, &reducer)) {
        if (key == self->union_type && reducer == self->trusted_union_reducer)
            continue;
        if (PyList_Append(keys, key) < 0) {
            Py_DECREF(keys);
            return -1;
        }
    }
    *out = keys;
    return 0;
}

static int registered_type(PyObject *keys, PyTypeObject *cls) {
    for (Py_ssize_t i = 0; i < PyList_Size(keys); i++) {
        if (PyList_GetItem(keys, i) == (PyObject *)cls)
            return 1;
    }
    return 0;
}

static int reject_reducer(void) {
    PyErr_SetString(PyExc_TypeError, "Registered copy reducers are not supported in copied data");
    return -1;
}

static int validate_namespace(Guard *self, PyObject *state) {
    if (!PyDict_CheckExact(state)) {
        PyErr_SetString(PyExc_TypeError, "Unsupported instance state in copied data");
        return -1;
    }
    Py_ssize_t position = 0;
    PyObject *key, *value;
    while (PyDict_Next(state, &position, &key, &value)) {
        if (!PyUnicode_CheckExact(key)) {
            PyErr_SetString(PyExc_TypeError, "Unsupported instance state in copied data");
            return -1;
        }
    }
    for (Py_ssize_t i = 0; i < PyTuple_Size(self->state_names); i++) {
        int present = PyDict_Contains(state, PyTuple_GetItem(self->state_names, i));
        if (present < 0)
            return -1;
        if (present) {
            PyErr_SetString(PyExc_TypeError,
                            "Instance state methods are not supported in copied data");
            return -1;
        }
    }
    PyObject *copier = PyDict_GetItemWithError(state, self->deepcopy_name);
    if (copier == NULL && PyErr_Occurred())
        return -1;
    if (copier != NULL && (PyObject *)Py_TYPE(copier) != self->function_type) {
        PyErr_SetString(PyExc_TypeError, "Instance copy methods must be ordinary Python functions");
        return -1;
    }
    return 0;
}

static int check_impl(Guard *self, PyObject *root) {
    PyObject *stack = PyList_New(0);
    PyObject *registered = NULL, *item = NULL;
    Identity seen = {0};
    Cache cache = {0};
    int status = -1;
    if (stack == NULL || registry_snapshot(self, &registered) < 0 || push(stack, root) < 0)
        goto done;
    while (PyList_Size(stack)) {
        Py_ssize_t last = PyList_Size(stack) - 1;
        item = Py_NewRef(PyList_GetItem(stack, last));
        if (PyList_SetSlice(stack, last, last + 1, NULL) < 0)
            goto done;
        int fresh = seen_add(&seen, item);
        if (fresh < 0)
            goto done;
        if (!fresh) {
            Py_CLEAR(item);
            continue;
        }
        PyTypeObject *cls = Py_TYPE(item);
        if (PyList_CheckExact(item) || PyTuple_CheckExact(item)) {
            Py_ssize_t size = PyList_CheckExact(item) ? PyList_Size(item) : PyTuple_Size(item);
            for (Py_ssize_t i = 0; i < size; i++) {
                PyObject *value =
                    PyList_CheckExact(item) ? PyList_GetItem(item, i) : PyTuple_GetItem(item, i);
                if (push(stack, value) < 0)
                    goto done;
            }
        } else if (PyDict_CheckExact(item)) {
            Py_ssize_t position = 0;
            PyObject *key, *value;
            while (PyDict_Next(item, &position, &key, &value)) {
                if (push(stack, key) < 0 || push(stack, value) < 0)
                    goto done;
            }
        } else if (PyType_Check(item) || (PyObject *)cls == self->function_type ||
                   (PyObject *)cls == self->builtin_function_type) {
            /* These references are atomic in stdlib deepcopy. */
        } else if (PySet_CheckExact(item) || PyFrozenSet_CheckExact(item)) {
            if (registered_type(registered, cls)) {
                reject_reducer();
                goto done;
            }
            PyObject *result = PyObject_CallFunctionObjArgs(self->fallback, item, NULL);
            if (result == NULL)
                goto done;
            Py_DECREF(result);
        } else {
            Entry *entry = cache_get(&cache, cls, self);
            if (entry == NULL)
                goto done;
            if (entry->info == Py_None) {
                if (entry->reference && registered_type(registered, cls)) {
                    reject_reducer();
                    goto done;
                }
                PyObject *result = PyObject_CallFunctionObjArgs(self->fallback, item, NULL);
                if (result == NULL)
                    goto done;
                Py_DECREF(result);
            } else {
                PyObject *kind = PyTuple_GetItem(entry->info, 0);
                PyObject *namespace = PyTuple_GetItem(entry->info, 1);
                PyObject *slots = PyTuple_GetItem(entry->info, 2);
                int state_kind = kind == self->kind_state;
                if (namespace != Py_None) {
                    PyObject *state = entry->nsget(namespace, item, (PyObject *)cls);
                    if (state == NULL)
                        goto done;
                    if (validate_namespace(self, state) < 0) {
                        Py_DECREF(state);
                        goto done;
                    }
                    if (state_kind) {
                        Py_ssize_t position = 0;
                        PyObject *key, *value;
                        while (PyDict_Next(state, &position, &key, &value)) {
                            if (push(stack, value) < 0) {
                                Py_DECREF(state);
                                goto done;
                            }
                        }
                    }
                    Py_DECREF(state);
                }
                if (registered_type(registered, cls)) {
                    PyObject *copier = PyObject_GetAttr(item, self->deepcopy_name);
                    if (copier == NULL) {
                        if (!PyErr_ExceptionMatches(PyExc_AttributeError))
                            goto done;
                        PyErr_Clear();
                        reject_reducer();
                        goto done;
                    }
                    int missing_copier = copier == Py_None;
                    Py_DECREF(copier);
                    if (missing_copier) {
                        reject_reducer();
                        goto done;
                    }
                }
                if (state_kind) {
                    for (Py_ssize_t i = 0; i < PyTuple_Size(slots); i++) {
                        PyObject *value =
                            entry->slotget(PyTuple_GetItem(slots, i), item, (PyObject *)cls);
                        if (value == NULL) {
                            if (!PyErr_ExceptionMatches(PyExc_AttributeError))
                                goto done;
                            PyErr_Clear();
                            continue;
                        }
                        int pushed = push(stack, value);
                        Py_DECREF(value);
                        if (pushed < 0)
                            goto done;
                    }
                }
            }
        }
        Py_CLEAR(item);
    }
    status = 0;
done:
    Py_XDECREF(item);
    Py_XDECREF(stack);
    Py_XDECREF(registered);
    cache_clear(&cache);
    seen_clear(&seen);
    return status;
}

static PyObject *guard_check(Guard *self, PyObject *value) {
    if (!guard_ready(self))
        return NULL;
    if (Py_EnterRecursiveCall(" while checking copied data"))
        return NULL;
    int status = check_impl(self, value);
    Py_LeaveRecursiveCall();
    if (status < 0)
        return NULL;
    Py_RETURN_NONE;
}

static PyObject *guard_class_info(Guard *self, PyObject *cls) {
    if (!guard_ready(self))
        return NULL;
    if (Py_EnterRecursiveCall(" while inspecting a copied type"))
        return NULL;
    PyObject *result = class_info_impl(self, cls);
    Py_LeaveRecursiveCall();
    return result;
}

static PyMethodDef guard_methods[] = {
    {"check", (PyCFunction)guard_check, METH_O,
     "check($self, value, /)\n--\n\nCheck supported data before an implicit copy."},
    {"class_info", (PyCFunction)guard_class_info, METH_O,
     "class_info($self, cls, /)\n--\n\nInspect static copy policy for a type."},
    {NULL, NULL, 0, NULL}};

static const char guard_doc[] =
    "_Guard(fallback, registry, trusted_union_reducer, union_type, function_type, "
    "builtin_function_type, getset_descriptor_type, member_descriptor_type, "
    "method_descriptor_type, wrapper_descriptor_type, struct_type, enum_type, atomic_ids, "
    "reference_ids, model_brands, native_state_methods, state_names, enum_namespace, missing, "
    "mro_descriptor, dict_descriptor, name_descriptor)\n--\n\n"
    "Native traversal and static policy checks for copied data.";

static PyType_Slot guard_slots[] = {
    {Py_tp_doc, (void *)guard_doc},   {Py_tp_new, PyType_GenericNew},
    {Py_tp_init, guard_init},         {Py_tp_dealloc, guard_dealloc},
    {Py_tp_traverse, guard_traverse}, {Py_tp_clear, guard_clear},
    {Py_tp_methods, guard_methods},   {0, NULL}};

static PyType_Spec guard_spec = {"msgspec_fastapi._defaults._Guard", sizeof(Guard), 0,
                                 Py_TPFLAGS_DEFAULT | Py_TPFLAGS_HAVE_GC | Py_TPFLAGS_IMMUTABLETYPE,
                                 guard_slots};

typedef struct {
    PyObject *guard_type;
    PyObject *copier_type;
} ModuleState;

PyObject *msgspec_fastapi_copier_type(PyObject *module);

static int module_traverse(PyObject *module, visitproc visit, void *arg) {
    ModuleState *state = PyModule_GetState(module);
    if (state != NULL) {
        Py_VISIT(state->guard_type);
        Py_VISIT(state->copier_type);
    }
    return 0;
}

static int module_clear(PyObject *module) {
    ModuleState *state = PyModule_GetState(module);
    if (state != NULL) {
        Py_CLEAR(state->guard_type);
        Py_CLEAR(state->copier_type);
    }
    return 0;
}

static void module_free(void *module) {
    module_clear((PyObject *)module);
}

static int module_exec(PyObject *module) {
    ModuleState *state = PyModule_GetState(module);
    if (state == NULL)
        return -1;
    if (state->guard_type != NULL)
        return 0;
    PyObject *type = PyType_FromModuleAndSpec(module, &guard_spec, NULL);
    if (type == NULL)
        return -1;
    PyObject *copier = msgspec_fastapi_copier_type(module);
    if (copier == NULL) {
        Py_DECREF(type);
        return -1;
    }
    if (PyModule_AddObjectRef(module, "_Guard", type) < 0 ||
        PyModule_AddObjectRef(module, "_Copier", copier) < 0) {
        Py_DECREF((PyObject *)type);
        Py_DECREF(copier);
        return -1;
    }
    state->guard_type = type;
    state->copier_type = copier;
    return 0;
}

static PyModuleDef_Slot module_slots[] = {{Py_mod_exec, module_exec}, {0, NULL}};

static struct PyModuleDef module_def = {PyModuleDef_HEAD_INIT,
                                        .m_name = "msgspec_fastapi._defaults",
                                        .m_doc =
                                            "Internal checks for copied defaults and metadata.",
                                        .m_size = sizeof(ModuleState),
                                        .m_slots = module_slots,
                                        .m_traverse = module_traverse,
                                        .m_clear = module_clear,
                                        .m_free = module_free};

PyMODINIT_FUNC PyInit__defaults(void) {
    return PyModuleDef_Init(&module_def);
}
