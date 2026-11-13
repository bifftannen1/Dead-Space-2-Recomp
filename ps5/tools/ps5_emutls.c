/* [ps5 port] The emulated TLS that ps5-plume-triangle's tools/radv-link.sh
 * writes as ps5-emutls-cxa.c (from LLVM compiler-rt's emutls.c, Apache-2.0
 * WITH LLVM-exception), compiled over it by ps5/tools/ps5_package.sh with one
 * fix: a C++ thread_local destructor run at thread exit that touches a
 * thread_local the thread never used grows (moves) the block, and the key
 * destructor went on with the old pointer (freed memory) each round. Nothing
 * else differs. */
#include <pthread.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>

/* Delay emutls deallocation and drain C++ thread_local destructors first
 * while emutls storage is still live on the exiting thread. */
#define EMUTLS_SKIP_DESTRUCTOR_ROUNDS 2

struct thread_destructor {
    void (*destructor)(void *);
    void *object;
    struct thread_destructor *next;
};

static pthread_key_t destructors_key;
static pthread_once_t destructors_once = PTHREAD_ONCE_INIT;
static bool destructors_ready;

static void
run_destructors(void *list)
{
    struct thread_destructor *entry = (struct thread_destructor *)list;
    while (entry) {
        struct thread_destructor *const next = entry->next;
        entry->destructor(entry->object);
        free(entry);
        struct thread_destructor *const added =
            (struct thread_destructor *)pthread_getspecific(destructors_key);
        if (added) {
            pthread_setspecific(destructors_key, NULL);
            run_destructors(added);
        }
        entry = next;
    }
}

static void
drain_thread_destructors(void)
{
    if (!destructors_ready)
        return;
    struct thread_destructor *const list =
        (struct thread_destructor *)pthread_getspecific(destructors_key);
    if (list) {
        pthread_setspecific(destructors_key, NULL);
        run_destructors(list);
    }
}

static void
setup_destructors(void)
{
    destructors_ready =
        pthread_key_create(&destructors_key, run_destructors) == 0 &&
        atexit(drain_thread_destructors) == 0;
}

__attribute__((visibility("hidden"))) int
ps5___cxa_thread_atexit_impl(void (*destructor)(void *), void *object, void *dso)
{
    (void)dso;
    pthread_once(&destructors_once, setup_destructors);
    if (!destructors_ready)
        return -1;
    struct thread_destructor *const entry =
        (struct thread_destructor *)malloc(sizeof(*entry));
    if (!entry)
        return -1;
    entry->destructor = destructor;
    entry->object = object;
    entry->next = (struct thread_destructor *)pthread_getspecific(destructors_key);
    if (pthread_setspecific(destructors_key, entry) != 0) {
        free(entry);
        return -1;
    }
    return 0;
}

typedef struct emutls_address_array {
    uintptr_t skip_destructor_rounds;
    uintptr_t size;
    void *data[];
} emutls_address_array;

typedef unsigned int gcc_word __attribute__((mode(word)));
typedef unsigned int gcc_pointer __attribute__((mode(pointer)));

typedef struct __emutls_control {
    gcc_word size;
    gcc_word align;
    union {
        uintptr_t index;
        void *address;
    } object;
    void *value;
} __emutls_control;

static pthread_mutex_t emutls_mutex = PTHREAD_MUTEX_INITIALIZER;
static pthread_key_t emutls_pthread_key;
static size_t emutls_num_object = 0;

static __inline void *
emutls_memalign_alloc(size_t align, size_t size)
{
    if (align < sizeof(void *))
        align = sizeof(void *);
    const size_t extra = align - 1 + sizeof(void *);
    char *const object = (char *)malloc(extra + size);
    if (!object)
        abort();
    void *const base =
        (void *)(((uintptr_t)(object + extra)) & ~(uintptr_t)(align - 1));
    ((void **)base)[-1] = object;
    return base;
}

static __inline void
emutls_memalign_free(void *base)
{
    free(((void **)base)[-1]);
}

static __inline void
emutls_setspecific(emutls_address_array *value)
{
    pthread_setspecific(emutls_pthread_key, (void *)value);
}

static __inline emutls_address_array *
emutls_getspecific(void)
{
    return (emutls_address_array *)pthread_getspecific(emutls_pthread_key);
}

static void
emutls_shutdown(emutls_address_array *array)
{
    if (array) {
        for (uintptr_t i = 0; i < array->size; ++i) {
            if (array->data[i]) {
                void *const ptr = array->data[i];
                array->data[i] = NULL;
                emutls_memalign_free(ptr);
            }
        }
    }
}

static void
emutls_key_destructor(void *ptr)
{
    emutls_address_array *array = (emutls_address_array *)ptr;
    if (destructors_ready && pthread_getspecific(destructors_key) != NULL) {
        emutls_setspecific(array);
        drain_thread_destructors();
        /* [ps5 port] A destructor may have grown (moved) the block. */
        array = emutls_getspecific();
        ptr = array;
    }
    if (array->skip_destructor_rounds > 0) {
        array->skip_destructor_rounds--;
        emutls_setspecific(array);
    } else {
        emutls_setspecific(NULL);
        emutls_shutdown(array);
        free(ptr);
    }
}

static void
emutls_init(void)
{
    if (pthread_key_create(&emutls_pthread_key, emutls_key_destructor) != 0)
        abort();
}

static __inline void
emutls_init_once(void)
{
    static pthread_once_t once = PTHREAD_ONCE_INIT;
    pthread_once(&once, emutls_init);
}

static __inline void *
emutls_allocate_object(__emutls_control *control)
{
    size_t size = control->size;
    size_t align = control->align;
    if (align < sizeof(void *))
        align = sizeof(void *);
    if ((align & (align - 1)) != 0)
        abort();
    void *base = emutls_memalign_alloc(align, size);
    if (control->value)
        memcpy(base, control->value, size);
    else
        memset(base, 0, size);
    return base;
}

static __inline uintptr_t
emutls_get_index(__emutls_control *control)
{
    uintptr_t index = __atomic_load_n(&control->object.index, __ATOMIC_ACQUIRE);
    if (!index) {
        emutls_init_once();
        pthread_mutex_lock(&emutls_mutex);
        index = control->object.index;
        if (!index) {
            index = ++emutls_num_object;
            __atomic_store_n(&control->object.index, index, __ATOMIC_RELEASE);
        }
        pthread_mutex_unlock(&emutls_mutex);
    }
    return index;
}

static __inline void
emutls_check_array_set_size(emutls_address_array *array, uintptr_t size)
{
    if (array == NULL)
        abort();
    array->size = size;
    emutls_setspecific(array);
}

static __inline uintptr_t
emutls_new_data_array_size(uintptr_t index)
{
    uintptr_t header_words = sizeof(emutls_address_array) / sizeof(void *);
    return ((index + header_words + 15) & ~((uintptr_t)15)) - header_words;
}

static __inline uintptr_t
emutls_asize(uintptr_t N)
{
    return N * sizeof(void *) + sizeof(emutls_address_array);
}

static __inline emutls_address_array *
emutls_get_address_array(uintptr_t index)
{
    emutls_address_array *array = emutls_getspecific();
    if (array == NULL) {
        uintptr_t new_size = emutls_new_data_array_size(index);
        array = (emutls_address_array *)malloc(emutls_asize(new_size));
        if (array) {
            memset(array->data, 0, new_size * sizeof(void *));
            array->skip_destructor_rounds = EMUTLS_SKIP_DESTRUCTOR_ROUNDS;
        }
        emutls_check_array_set_size(array, new_size);
    } else if (index > array->size) {
        uintptr_t orig_size = array->size;
        uintptr_t new_size = emutls_new_data_array_size(index);
        array = (emutls_address_array *)realloc(array, emutls_asize(new_size));
        if (array)
            memset(array->data + orig_size, 0,
                   (new_size - orig_size) * sizeof(void *));
        emutls_check_array_set_size(array, new_size);
    }
    return array;
}

__attribute__((visibility("hidden"))) void *
__emutls_get_address(__emutls_control *control)
{
    uintptr_t index = emutls_get_index(control);
    emutls_address_array *array = emutls_get_address_array(index--);
    if (array->data[index] == NULL)
        array->data[index] = emutls_allocate_object(control);
    return array->data[index];
}
