#include <Python.h>

#define COPIER_POLICY_FIELDS(X)                                                                    \
    X(module)                                                                                      \
    X(fallback)                                                                                    \
    X(list_copier) X(dict_copier) X(tuple_copier) X(atomic_copier) X(original_reconstruct)         \
        X(memo_sentinel)
#define COPIER_NAME_FIELDS(X)                                                                      \
    X(dispatch_name)                                                                               \
    X(registry_name)                                                                               \
    X(atomicset_name)                                                                              \
    X(keepalive_name)                                                                              \
    X(reconstruct_name)                                                                            \
    X(copy_name) X(reduce_name) X(reduceex_name) X(items_name) X(get_name) X(setstate_name)        \
        X(namespace_name) X(update_name)

typedef struct {
    PyObject_HEAD
#define OWNED_FIELD(name) PyObject *name;
    COPIER_POLICY_FIELDS(OWNED_FIELD)
    COPIER_NAME_FIELDS(OWNED_FIELD)
#undef OWNED_FIELD
    int atomic_first, initialized;
} Copier;

typedef struct {
    Copier *owner;
    PyObject *memo;
} Context;
static PyObject *clone_value(Context *, PyObject *);

static void replace_owned(PyObject **slot, PyObject *next) {
    PyObject *old = *slot;
    *slot = next;
    Py_XDECREF(old);
}

static PyObject *module_value(Context *c, PyObject *name) {
    PyObject *value = PyDict_GetItemWithError(PyModule_GetDict(c->owner->module), name);
    if (value == NULL && !PyErr_Occurred())
        PyErr_SetObject(PyExc_AttributeError, name);
    return value == NULL ? NULL : Py_NewRef(value);
}

static PyObject *id_key(PyObject *value) {
    return PyLong_FromVoidPtr(value);
}

static PyObject *memo_get(Context *c, PyObject *value) {
    PyObject *key = id_key(value);
    if (key == NULL)
        return NULL;
    PyObject *out = PyDict_GetItemWithError(c->memo, key);
    Py_XINCREF(out);
    Py_DECREF(key);
    return out;
}

static int memo_set(Context *c, PyObject *value, PyObject *result) {
    PyObject *key = id_key(value);
    if (key == NULL)
        return -1;
    int out = PyDict_SetItem(c->memo, key, result);
    Py_DECREF(key);
    return out;
}

static int keep_alive(Context *c, PyObject *value) {
    PyObject *function = module_value(c, c->owner->keepalive_name);
    if (function == NULL)
        return -1;
    PyObject *result = PyObject_CallFunctionObjArgs(function, value, c->memo, NULL);
    Py_DECREF(function);
    if (result == NULL)
        return -1;
    Py_DECREF(result);
    return 0;
}

static int atomic(PyObject *x) {
    return x == Py_None || PyBool_Check(x) || PyLong_CheckExact(x) || PyFloat_CheckExact(x) ||
           PyUnicode_CheckExact(x) || PyBytes_CheckExact(x);
}

static PyObject *clone_list(Context *c, PyObject *x) {
    PyObject *out = PyList_New(0), *item = NULL;
    if (out == NULL)
        return NULL;
    if (memo_set(c, x, out) < 0)
        goto failed;
    for (Py_ssize_t i = 0; i < PyList_Size(x); i++) {
        PyObject *next = Py_NewRef(PyList_GetItem(x, i));
        replace_owned(&item, next);
        PyObject *value = clone_value(c, item);
        if (value == NULL)
            goto failed;
        int status = PyList_Append(out, value);
        Py_DECREF(value);
        if (status < 0)
            goto failed;
    }
    Py_XDECREF(item);
    return out;
failed:
    Py_XDECREF(item);
    Py_DECREF(out);
    return NULL;
}

static PyObject *clone_tuple(Context *c, PyObject *x) {
    Py_ssize_t n = PyTuple_Size(x);
    PyObject *out = PyList_New(0);
    if (out == NULL)
        return NULL;
    int changed = 0;
    for (Py_ssize_t i = 0; i < n; i++) {
        PyObject *item = PyTuple_GetItem(x, i);
        PyObject *value = clone_value(c, item);
        if (value == NULL) {
            Py_DECREF(out);
            return NULL;
        }
        if (item != value)
            changed = 1;
        int status = PyList_Append(out, value);
        Py_DECREF(value);
        if (status < 0) {
            Py_DECREF(out);
            return NULL;
        }
    }
    PyObject *cached = memo_get(c, x);
    if (cached != NULL) {
        Py_DECREF(out);
        return cached;
    }
    if (PyErr_Occurred()) {
        Py_DECREF(out);
        return NULL;
    }
    if (!changed) {
        Py_DECREF(out);
        return Py_NewRef(x);
    }
    PyObject *result = PyList_AsTuple(out);
    Py_DECREF(out);
    return result;
}

static PyObject *clone_dict(Context *c, PyObject *x) {
    PyObject *out = PyDict_New(), *iter = NULL, *key_local = NULL, *value_local = NULL;
    if (out == NULL)
        return NULL;
    if (memo_set(c, x, out) < 0)
        goto failed;
    PyObject *items = PyObject_CallMethodObjArgs(x, c->owner->items_name, NULL);
    if (items == NULL)
        goto failed;
    iter = PyObject_GetIter(items);
    Py_DECREF(items);
    if (iter == NULL)
        goto failed;
    PyObject *pair;
    while ((pair = PyIter_Next(iter)) != NULL) {
        PyObject *next_key = Py_NewRef(PyTuple_GetItem(pair, 0));
        PyObject *next_value = Py_NewRef(PyTuple_GetItem(pair, 1));
        replace_owned(&key_local, next_key);
        replace_owned(&value_local, next_value);
        Py_DECREF(pair);
        PyObject *value = clone_value(c, value_local);
        PyObject *key = value == NULL ? NULL : clone_value(c, key_local);
        if (key == NULL) {
            Py_XDECREF(value);
            goto failed;
        }
        int status = PyObject_SetItem(out, key, value);
        Py_DECREF(key);
        Py_DECREF(value);
        if (status < 0)
            goto failed;
    }
    if (PyErr_Occurred())
        goto failed;
    Py_DECREF(iter);
    if (c->owner->atomic_first) {
        Py_XDECREF(value_local);
        Py_XDECREF(key_local);
    } else {
        Py_XDECREF(key_local);
        Py_XDECREF(value_local);
    }
    return out;
failed:
    Py_XDECREF(iter);
    if (c->owner->atomic_first) {
        Py_XDECREF(value_local);
        Py_XDECREF(key_local);
    } else {
        Py_XDECREF(key_local);
        Py_XDECREF(value_local);
    }
    Py_DECREF(out);
    return NULL;
}

static PyObject *optional_attr(Context *c, PyObject *x, PyObject *name) {
    (void)c;
    PyObject *out = PyObject_GetAttr(x, name);
    if (out == NULL && PyErr_ExceptionMatches(PyExc_AttributeError)) {
        PyErr_Clear();
        return Py_NewRef(Py_None);
    }
    return out;
}

typedef struct {
    PyObject *x, *memo, *d, *y, *cls, *copier, *reductor, *rv;
} CopyFrame;

static void frame_clear(Context *c, CopyFrame *frame) {
    if (c->owner->atomic_first) {
        Py_CLEAR(frame->rv);
        Py_CLEAR(frame->reductor);
        Py_CLEAR(frame->copier);
        Py_CLEAR(frame->y);
        Py_CLEAR(frame->d);
        Py_CLEAR(frame->cls);
        Py_CLEAR(frame->memo);
        Py_CLEAR(frame->x);
    } else {
        Py_CLEAR(frame->x);
        Py_CLEAR(frame->memo);
        Py_CLEAR(frame->d);
        Py_CLEAR(frame->y);
        Py_CLEAR(frame->cls);
        Py_CLEAR(frame->copier);
        Py_CLEAR(frame->reductor);
        Py_CLEAR(frame->rv);
    }
}

/* The source helper's state branch remains dynamic after construction. */
typedef struct {
    PyObject *state, *y, *slotstate, *key, *value;
} StateFrame;

static void state_frame_clear(Context *c, StateFrame *frame) {
    if (c->owner->atomic_first) {
        Py_CLEAR(frame->value);
        Py_CLEAR(frame->key);
        Py_CLEAR(frame->slotstate);
        Py_CLEAR(frame->y);
        Py_CLEAR(frame->state);
    } else {
        Py_CLEAR(frame->state);
        Py_CLEAR(frame->y);
        Py_CLEAR(frame->slotstate);
        Py_CLEAR(frame->key);
        Py_CLEAR(frame->value);
    }
}

static int unpack_pair(Context *c, PyObject *source, PyObject **first, PyObject **second) {
    PyObject *iter = PyObject_GetIter(source);
    if (iter == NULL) {
        if (PyErr_ExceptionMatches(PyExc_TypeError) &&
            PyType_GetSlot(Py_TYPE(source), Py_tp_iter) == NULL && !PySequence_Check(source)) {
            PyObject *name = PyType_GetName(Py_TYPE(source));
            if (name != NULL) {
                PyErr_Format(PyExc_TypeError, "cannot unpack non-iterable %U object", name);
                Py_DECREF(name);
            }
        }
        return -1;
    }
    PyObject *a = PyIter_Next(iter), *b = NULL, *extra = NULL;
    int count = 0;
    if (a != NULL) {
        count = 1;
        b = PyIter_Next(iter);
    }
    if (b != NULL) {
        count = 2;
        extra = PyIter_Next(iter);
    }
    if (PyErr_Occurred())
        goto failed;
    if (extra != NULL) {
        Py_CLEAR(extra);
        Py_ssize_t size = -1;
        if (c->owner->atomic_first) {
            if (PyList_CheckExact(source))
                size = PyList_Size(source);
            else if (PyTuple_CheckExact(source))
                size = PyTuple_Size(source);
            else if (PyDict_CheckExact(source))
                size = PyDict_Size(source);
        }
        if (size > 2)
            PyErr_Format(PyExc_ValueError, "too many values to unpack (expected 2, got %zd)", size);
        else
            PyErr_SetString(PyExc_ValueError, "too many values to unpack (expected 2)");
        goto failed;
    }
    if (count < 2) {
        PyErr_Format(PyExc_ValueError, "not enough values to unpack (expected 2, got %d)", count);
        goto failed;
    }
    Py_DECREF(iter);
    *first = a;
    *second = b;
    return 0;
failed:
    Py_XDECREF(extra);
    Py_XDECREF(b);
    Py_XDECREF(a);
    Py_DECREF(iter);
    return -1;
}

static void argument_stopiteration(void) {
    if (!PyErr_ExceptionMatches(PyExc_StopIteration))
        return;
    PyObject *type = NULL, *value = NULL, *traceback = NULL;
    PyErr_Fetch(&type, &value, &traceback);
    PyErr_NormalizeException(&type, &value, &traceback);
    if (!PyErr_Occurred() && value != NULL &&
        PyException_SetTraceback(value, traceback == NULL ? Py_None : traceback) == 0) {
        PyObject *error =
            PyObject_CallFunction(PyExc_RuntimeError, "s", "generator raised StopIteration");
        if (error != NULL) {
            PyException_SetCause(error, Py_NewRef(value));
            PyException_SetContext(error, Py_NewRef(value));
            PyErr_SetObject(PyExc_RuntimeError, error);
            Py_DECREF(error);
        }
    }
    Py_XDECREF(type);
    Py_XDECREF(value);
    Py_XDECREF(traceback);
}

static PyObject *construct(Context *c, CopyFrame *frame) {
    PyObject *args = PyTuple_GetItem(frame->rv, 1);
    PyObject *copied = PyList_New(0);
    if (copied == NULL)
        return NULL;
    for (Py_ssize_t i = 0; i < PyTuple_Size(args); i++) {
        PyObject *value = clone_value(c, PyTuple_GetItem(args, i));
        if (value == NULL) {
            argument_stopiteration();
            Py_DECREF(copied);
            return NULL;
        }
        int status = PyList_Append(copied, value);
        Py_DECREF(value);
        if (status < 0) {
            Py_DECREF(copied);
            return NULL;
        }
    }
    PyObject *call_args = PyList_AsTuple(copied);
    Py_DECREF(copied);
    if (call_args == NULL)
        return NULL;
    PyObject *out = PyObject_CallObject(PyTuple_GetItem(frame->rv, 0), call_args);
    Py_DECREF(call_args);
    if (out != NULL && memo_set(c, frame->x, out) < 0)
        Py_CLEAR(out);
    return out;
}

static PyObject *reconstruct_state(Context *c, CopyFrame *frame) {
    StateFrame state = {0};
    state.state = Py_NewRef(PyTuple_GetItem(frame->rv, 2));
    state.y = construct(c, frame);
    if (state.y == NULL)
        goto done;
    PyObject *copied_state = clone_value(c, state.state);
    if (copied_state == NULL)
        goto done;
    replace_owned(&state.state, copied_state);
    PyObject *probe = PyObject_GetAttr(state.y, c->owner->setstate_name);
    if (probe != NULL) {
        Py_DECREF(probe);
        PyObject *function = PyObject_GetAttr(state.y, c->owner->setstate_name);
        if (function == NULL)
            goto done;
        PyObject *result = PyObject_CallFunctionObjArgs(function, state.state, NULL);
        Py_DECREF(function);
        if (result == NULL)
            goto done;
        Py_DECREF(result);
    } else {
        if (!PyErr_ExceptionMatches(PyExc_AttributeError))
            goto done;
        PyErr_Clear();
        int tuple_state = PyObject_IsInstance(state.state, (PyObject *)&PyTuple_Type);
        if (tuple_state < 0)
            goto done;
        Py_ssize_t length = tuple_state ? PyObject_Length(state.state) : -2;
        if (length == -1)
            goto done;
        if (length == 2) {
            PyObject *first = NULL, *second = NULL;
            if (unpack_pair(c, state.state, &first, &second) < 0)
                goto done;
            replace_owned(&state.state, first);
            state.slotstate = second;
        } else
            state.slotstate = Py_NewRef(Py_None);
        if (state.state != Py_None) {
            PyObject *namespace = PyObject_GetAttr(state.y, c->owner->namespace_name);
            if (namespace == NULL)
                goto done;
            PyObject *result =
                PyObject_CallMethodObjArgs(namespace, c->owner->update_name, state.state, NULL);
            Py_DECREF(namespace);
            if (result == NULL)
                goto done;
            Py_DECREF(result);
        }
        if (state.slotstate != Py_None) {
            PyObject *items =
                PyObject_CallMethodObjArgs(state.slotstate, c->owner->items_name, NULL);
            if (items == NULL)
                goto done;
            PyObject *iter = PyObject_GetIter(items);
            Py_DECREF(items);
            if (iter == NULL)
                goto done;
            PyObject *pair;
            while ((pair = PyIter_Next(iter)) != NULL) {
                PyObject *first = NULL, *second = NULL;
                int status = unpack_pair(c, pair, &first, &second);
                Py_DECREF(pair);
                if (status < 0) {
                    Py_DECREF(iter);
                    goto done;
                }
                replace_owned(&state.key, first);
                replace_owned(&state.value, second);
                if (PyObject_SetAttr(state.y, state.key, state.value) < 0) {
                    Py_DECREF(iter);
                    goto done;
                }
            }
            Py_DECREF(iter);
            if (PyErr_Occurred())
                goto done;
        }
    }
    PyObject *out = Py_NewRef(state.y);
    state_frame_clear(c, &state);
    return out;
done:
    state_frame_clear(c, &state);
    return NULL;
}

static PyObject *reconstruct(Context *c, CopyFrame *frame) {
    PyObject *out = NULL;
    PyObject *x = frame->x, *rv = frame->rv;
    PyObject *reconstruct = module_value(c, c->owner->reconstruct_name);
    if (reconstruct == NULL) {
        return NULL;
    }
    int standard = reconstruct == c->owner->original_reconstruct;
    Py_ssize_t rv_size = PyTuple_CheckExact(rv) ? PyTuple_Size(rv) : -1;
    int state_shape = rv_size == 3 || (rv_size == 5 && PyTuple_GetItem(rv, 3) == Py_None &&
                                       PyTuple_GetItem(rv, 4) == Py_None);
    if (standard && state_shape && PyTuple_CheckExact(PyTuple_GetItem(rv, 1)) &&
        PyDict_CheckExact(PyTuple_GetItem(rv, 2)))
        out = reconstruct_state(c, frame);
    else if (standard && rv_size == 2 && PyTuple_CheckExact(PyTuple_GetItem(rv, 1))) {
        out = construct(c, frame);
    } else {
        PyObject *sequence = PySequence_Tuple(rv);
        if (sequence == NULL)
            goto reconstruct_done;
        Py_ssize_t n = PyTuple_Size(sequence);
        if (n > PY_SSIZE_T_MAX - 2) {
            Py_DECREF(sequence);
            PyErr_NoMemory();
            goto reconstruct_done;
        }
        PyObject *args = PyTuple_New(n + 2);
        if (args == NULL) {
            Py_DECREF(sequence);
            goto reconstruct_done;
        }
        PyTuple_SetItem(args, 0, Py_NewRef(x));
        PyTuple_SetItem(args, 1, Py_NewRef(c->memo));
        for (Py_ssize_t i = 0; i < n; i++)
            PyTuple_SetItem(args, i + 2, Py_NewRef(PyTuple_GetItem(sequence, i)));
        if (standard) {
            PyObject *callback = Py_NewRef((PyObject *)c->owner);
            PyObject *kwargs =
                callback == NULL ? NULL : Py_BuildValue("{sO}", "deepcopy", callback);
            out = kwargs == NULL ? NULL : PyObject_Call(reconstruct, args, kwargs);
            Py_XDECREF(kwargs);
            Py_XDECREF(callback);
        } else
            out = PyObject_CallObject(reconstruct, args);
        Py_DECREF(args);
        Py_DECREF(sequence);
    }
reconstruct_done:
    Py_DECREF(reconstruct);
    return out;
}

static PyObject *mapping_get(PyObject *mapping, PyObject *key, PyObject *get_name) {
    if (PyDict_CheckExact(mapping)) {
        PyObject *out = PyDict_GetItemWithError(mapping, key);
        if (out != NULL)
            return Py_NewRef(out);
        if (PyErr_Occurred())
            return NULL;
        return Py_NewRef(Py_None);
    }
    return PyObject_CallMethodObjArgs(mapping, get_name, key, NULL);
}

static PyObject *clone_impl(Context *c, PyObject *x) {
    CopyFrame f = {0};
    f.x = Py_NewRef(x);
    f.memo = Py_NewRef(c->memo);
    if (c->owner->atomic_first) {
        f.cls = Py_NewRef((PyObject *)Py_TYPE(x));
        PyObject *types = module_value(c, c->owner->atomicset_name);
        if (types == NULL)
            goto done;
        int found = PySequence_Contains(types, f.cls);
        Py_DECREF(types);
        if (found < 0)
            goto done;
        if (found) {
            f.y = Py_NewRef(x);
            goto done;
        }
    }
    f.d = id_key(x);
    if (f.d == NULL)
        goto done;
    PyObject *cached = PyDict_GetItemWithError(c->memo, f.d);
    if (cached != NULL && cached != c->owner->memo_sentinel) {
        f.y = Py_NewRef(cached);
        goto done;
    }
    if (PyErr_Occurred())
        goto done;
    if (f.cls == NULL)
        f.cls = Py_NewRef((PyObject *)Py_TYPE(x));
    PyObject *table = module_value(c, c->owner->dispatch_name);
    if (table == NULL)
        goto done;
    f.copier = mapping_get(table, f.cls, c->owner->get_name);
    Py_DECREF(table);
    if (f.copier == NULL)
        goto done;
    if (f.copier != Py_None) {
        if (f.copier == c->owner->atomic_copier && atomic(x))
            f.y = Py_NewRef(x);
        else if (f.cls == (PyObject *)&PyList_Type && f.copier == c->owner->list_copier)
            f.y = clone_list(c, x);
        else if (f.cls == (PyObject *)&PyDict_Type && f.copier == c->owner->dict_copier)
            f.y = clone_dict(c, x);
        else if (f.cls == (PyObject *)&PyTuple_Type && f.copier == c->owner->tuple_copier)
            f.y = clone_tuple(c, x);
        else
            f.y = PyObject_CallFunctionObjArgs(f.copier, x, c->memo, NULL);
    } else if (PyType_IsSubtype((PyTypeObject *)f.cls, &PyType_Type))
        f.y = Py_NewRef(x);
    else {
        PyObject *next = optional_attr(c, x, c->owner->copy_name);
        if (next == NULL)
            goto done;
        replace_owned(&f.copier, next);
        if (f.copier != Py_None)
            f.y = PyObject_CallFunctionObjArgs(f.copier, c->memo, NULL);
        else {
            PyObject *registry = module_value(c, c->owner->registry_name);
            if (registry == NULL)
                goto done;
            f.reductor = mapping_get(registry, f.cls, c->owner->get_name);
            Py_DECREF(registry);
            if (f.reductor == NULL)
                goto done;
            int present = PyObject_IsTrue(f.reductor);
            if (present < 0)
                goto done;
            if (present)
                f.rv = PyObject_CallFunctionObjArgs(f.reductor, x, NULL);
            else {
                next = optional_attr(c, x, c->owner->reduceex_name);
                if (next == NULL)
                    goto done;
                replace_owned(&f.reductor, next);
                if (f.reductor != Py_None)
                    f.rv = PyObject_CallFunction(f.reductor, "i", 4);
                else {
                    next = optional_attr(c, x, c->owner->reduce_name);
                    if (next == NULL)
                        goto done;
                    replace_owned(&f.reductor, next);
                    present = PyObject_IsTrue(f.reductor);
                    if (present < 0)
                        goto done;
                    if (present)
                        f.rv = PyObject_CallFunctionObjArgs(f.reductor, NULL);
                    else {
                        PyObject *error =
                            PyDict_GetItemString(PyModule_GetDict(c->owner->module), "Error");
                        if (error != NULL)
                            PyErr_Format(error, "un(deep)copyable object of type %S", f.cls);
                        else
                            PyErr_SetString(PyExc_TypeError, "Missing copy.Error");
                    }
                }
            }
            if (f.rv != NULL) {
                int string_rv = PyObject_IsInstance(f.rv, (PyObject *)&PyUnicode_Type);
                if (string_rv < 0)
                    goto done;
                if (string_rv)
                    f.y = Py_NewRef(x);
                else
                    f.y = reconstruct(c, &f);
            }
        }
    }
    if (f.y != NULL && f.y != x) {
        if (PyDict_SetItem(c->memo, f.d, f.y) < 0 || keep_alive(c, x) < 0)
            Py_CLEAR(f.y);
    }
done:;
    PyObject *out = f.y == NULL ? NULL : Py_NewRef(f.y);
    frame_clear(c, &f);
    return out;
}

static PyObject *clone_value(Context *c, PyObject *x) {
    if (Py_EnterRecursiveCall(" while copying msgspec defaults"))
        return NULL;
    PyObject *out = clone_impl(c, x);
    Py_LeaveRecursiveCall();
    return out;
}

static int copier_ready(Copier *self) {
    if (!self->initialized || self->module == NULL) {
        PyErr_SetString(PyExc_TypeError, "Copier is not initialized");
        return 0;
    }
    return 1;
}

static PyObject *copier_copy(Copier *self, PyObject *args) {
    PyObject *value, *memo = Py_None;
    if (!copier_ready(self))
        return NULL;
    if (!PyArg_ParseTuple(args, "O|O:deepcopy", &value, &memo))
        return NULL;
    if (memo == Py_None)
        memo = PyDict_New();
    else
        Py_INCREF(memo);
    if (memo == NULL)
        return NULL;
    if (!PyDict_CheckExact(memo)) {
        PyObject *out = PyObject_CallFunctionObjArgs(self->fallback, value, memo, NULL);
        Py_DECREF(memo);
        return out;
    }
    Context current = {self, memo};
    PyObject *out = clone_value(&current, value);
    Py_DECREF(memo);
    return out;
}

static PyObject *copier_call(Copier *self, PyObject *args, PyObject *kwargs) {
    if (kwargs != NULL && PyDict_Size(kwargs)) {
        PyErr_SetString(PyExc_TypeError, "deepcopy accepts positional arguments only");
        return NULL;
    }
    return copier_copy(self, args);
}

static int copier_traverse(Copier *self, visitproc visit, void *arg) {
    Py_VISIT(Py_TYPE((PyObject *)self));
#define VISIT_FIELD(name) Py_VISIT(self->name);
    COPIER_POLICY_FIELDS(VISIT_FIELD)
    COPIER_NAME_FIELDS(VISIT_FIELD)
#undef VISIT_FIELD
    return 0;
}

static int copier_clear(Copier *self) {
#define CLEAR_FIELD(name) Py_CLEAR(self->name);
    COPIER_POLICY_FIELDS(CLEAR_FIELD)
    COPIER_NAME_FIELDS(CLEAR_FIELD)
#undef CLEAR_FIELD
    return 0;
}

static void copier_dealloc(Copier *self) {
    PyTypeObject *type = Py_TYPE((PyObject *)self);
    PyObject_GC_UnTrack(self);
    copier_clear(self);
    ((freefunc)PyType_GetSlot(type, Py_tp_free))(self);
    Py_DECREF(type);
}

static PyObject *copier_new(PyTypeObject *type, PyObject *args, PyObject *kwargs) {
    (void)args;
    (void)kwargs;
    return PyType_GenericAlloc(type, 0);
}

static int copier_init(Copier *self, PyObject *args, PyObject *kwargs) {
    static char *keywords[] = {
        "copy_module",   "deepcopy",    "list_copier",  "dict_copier",   "tuple_copier",
        "atomic_copier", "reconstruct", "atomic_first", "memo_sentinel", NULL};
    PyObject *module, *fallback, *list_copier, *dict_copier, *tuple_copier;
    PyObject *atomic_copier, *original_reconstruct, *atomic_first, *memo_sentinel;
    if (self->initialized) {
        PyErr_SetString(PyExc_TypeError, "Copier cannot be reinitialized");
        return -1;
    }
    if (!PyArg_ParseTupleAndKeywords(args, kwargs, "OOOOOOOOO:_Copier", keywords, &module,
                                     &fallback, &list_copier, &dict_copier, &tuple_copier,
                                     &atomic_copier, &original_reconstruct, &atomic_first,
                                     &memo_sentinel))
        return -1;
    if (!PyModule_Check(module)) {
        PyErr_SetString(PyExc_TypeError, "copy_module must be a module");
        return -1;
    }
    PyObject *functions[] = {fallback, list_copier, dict_copier, tuple_copier,
                             original_reconstruct};
    for (size_t i = 0; i < sizeof(functions) / sizeof(functions[0]); i++) {
        if (!PyCallable_Check(functions[i])) {
            PyErr_SetString(PyExc_TypeError, "Copy helpers must be callable");
            return -1;
        }
    }
    if (atomic_copier != Py_None && !PyCallable_Check(atomic_copier)) {
        PyErr_SetString(PyExc_TypeError, "atomic_copier must be callable or None");
        return -1;
    }
    if (!PyBool_Check(atomic_first)) {
        PyErr_SetString(PyExc_TypeError, "atomic_first must be a bool");
        return -1;
    }
#define SAVE_FIELD(name) self->name = Py_NewRef(name);
    COPIER_POLICY_FIELDS(SAVE_FIELD)
#undef SAVE_FIELD
    self->atomic_first = atomic_first == Py_True;
#define SAVE_NAME(field, name)                                                                     \
    do {                                                                                           \
        self->field = PyUnicode_InternFromString(name);                                            \
        if (self->field == NULL) {                                                                 \
            copier_clear(self);                                                                    \
            return -1;                                                                             \
        }                                                                                          \
    } while (0)
    SAVE_NAME(dispatch_name, "_deepcopy_dispatch");
    SAVE_NAME(registry_name, "dispatch_table");
    SAVE_NAME(atomicset_name, "_atomic_types");
    SAVE_NAME(keepalive_name, "_keep_alive");
    SAVE_NAME(reconstruct_name, "_reconstruct");
    SAVE_NAME(copy_name, "__deepcopy__");
    SAVE_NAME(reduce_name, "__reduce__");
    SAVE_NAME(reduceex_name, "__reduce_ex__");
    SAVE_NAME(items_name, "items");
    SAVE_NAME(get_name, "get");
    SAVE_NAME(setstate_name, "__setstate__");
    SAVE_NAME(namespace_name, "__dict__");
    SAVE_NAME(update_name, "update");
#undef SAVE_NAME
    self->initialized = 1;
    return 0;
}

static PyMethodDef copier_methods[] = {
    {"deepcopy", (PyCFunction)copier_copy, METH_VARARGS,
     "deepcopy($self, value, memo=None, /)\n--\n\nCopy data using a shared deepcopy memo."},
    {NULL, NULL, 0, NULL}};
static PyType_Slot copier_slots[] = {
    {Py_tp_new, copier_new},
    {Py_tp_init, copier_init},
    {Py_tp_call, copier_call},
    {Py_tp_dealloc, copier_dealloc},
    {Py_tp_traverse, copier_traverse},
    {Py_tp_clear, copier_clear},
    {Py_tp_methods, copier_methods},
    {Py_tp_doc, "_Copier(copy_module, deepcopy, list_copier, dict_copier, tuple_copier, "
                "atomic_copier, reconstruct, atomic_first, memo_sentinel)"
                "\n--\n\nCopy supported defaults with stdlib protocol ordering."},
    {0, NULL}};
static PyType_Spec copier_spec = {
    "msgspec_fastapi._defaults._Copier", sizeof(Copier), 0,
    Py_TPFLAGS_DEFAULT | Py_TPFLAGS_HAVE_GC | Py_TPFLAGS_IMMUTABLETYPE, copier_slots};

PyObject *msgspec_fastapi_copier_type(PyObject *module) {
    return PyType_FromModuleAndSpec(module, &copier_spec, NULL);
}
