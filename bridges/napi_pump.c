/* AppCtl event-loop pump. Stable Node-API C ABI, no account/app operations. */
#include <windows.h>
#include <stdint.h>
#ifndef APPCTL_NAPI_HOST
#define APPCTL_NAPI_HOST NULL
#endif
typedef void* env_t;
typedef void* val_t;
typedef val_t (*callback_t)(env_t, void*);
typedef int (*cb_info_t)(env_t, void*, size_t*, val_t*, val_t*, void**);
typedef int (*create_fn_t)(env_t, const char*, size_t, callback_t, void*, val_t*);
typedef int (*set_t)(env_t, val_t, const char*, val_t);
typedef int (*number_t)(env_t, uint32_t, val_t*);
static cb_info_t cb_info;
static number_t number;
static val_t tick(env_t env, void* info) {
    size_t argc = 0;
    cb_info(env, info, &argc, NULL, NULL, NULL);
    val_t result;
    number(env, GetCurrentThreadId(), &result);
    return result;
}
__declspec(dllexport) val_t napi_register_module_v1(env_t env, val_t exports) {
    HMODULE main = GetModuleHandleA(APPCTL_NAPI_HOST);
    cb_info = (cb_info_t)GetProcAddress(main, "napi_get_cb_info");
    number = (number_t)GetProcAddress(main, "napi_create_uint32");
    create_fn_t create_fn = (create_fn_t)GetProcAddress(main, "napi_create_function");
    set_t set = (set_t)GetProcAddress(main, "napi_set_named_property");
    if (!cb_info || !number || !create_fn || !set) return exports;
    val_t fn;
    create_fn(env, "tick", 4, tick, NULL, &fn);
    set(env, exports, "tick", fn);
    return exports;
}
