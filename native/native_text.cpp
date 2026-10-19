// Original implementation derived from local static analysis of 4.1.15.51.
// Experimental: callback success means local queue acceptance, not delivery.
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <cstdint>
#include <cstring>
#include <cstddef>
#include <cstdio>
#include <functional>
#include <memory>

struct Request {
    uint32_t magic, size, operation, reserved;
    char recipient[256], allowed_recipient[256], text[8192], request_id[64];
    volatile LONG status;
    char error[512];
};
static_assert(sizeof(Request) == 9300);
struct String {
    union { char small[16]; const char* ptr; } data;
    size_t size, capacity;
};
static_assert(sizeof(String) == 32);
struct TextParams { String recipient, text; };
struct Callback {
    volatile LONG refs;
    uint32_t padding;
    void (*invoke)(Callback*, const unsigned char*);
    void (*destroy)(Callback*);
    HANDLE event;
    volatile LONG status;
    char error[512];
};
static_assert(offsetof(Callback, invoke) == 8 && offsetof(Callback, destroy) == 16);

static void Destroy(Callback* self) {
    CloseHandle(self->event);
    HeapFree(GetProcessHeap(), 0, self);
}
static void Release(Callback* self) {
    if (InterlockedDecrement(&self->refs) == 0) Destroy(self);
}
static void Receive(Callback* self, const unsigned char* result) {
    // The original callback consumes a bool and an optional std::string.
    // Never dereference arbitrary result pointers on an unexpected layout.
    __try {
        self->status = result[0] == 1 ? 1 : 2;
        if (result[0] != 0 && result[0] != 1) self->status = 5;
        if (self->status == 2 && result[40] == 1) {
            const auto* str = reinterpret_cast<const String*>(result + 8);
            if (str->size < sizeof(self->error) && str->capacity >= str->size) {
                const char* bytes = str->capacity < 16 ? str->data.small : str->data.ptr;
                memcpy(self->error, bytes, str->size);
                self->error[str->size] = 0;
            }
        }
    } __except(EXCEPTION_EXECUTE_HANDLER) {
        self->status = 5;
    }
    SetEvent(self->event);
}
static String Borrow(const char* value) {
    String str{};
    str.size = strlen(value);
    str.capacity = str.size < 16 ? 15 : str.size;
    if (str.size < 16) memcpy(str.data.small, value, str.size + 1);
    else str.data.ptr = value;
    return str;
}
static bool Terminated(const char* value, size_t capacity) {
    return memchr(value, 0, capacity) != nullptr;
}
static bool Validate(Request* r) {
    if (!r || r->magic != 0x57414232 || r->size != sizeof(Request)) return false;
    if (r->operation != 1 && r->operation != 2 && r->operation != 3) return false;
    if (!Terminated(r->recipient, sizeof(r->recipient)) ||
        !Terminated(r->allowed_recipient, sizeof(r->allowed_recipient)) ||
        !Terminated(r->text, sizeof(r->text)) ||
        !Terminated(r->request_id, sizeof(r->request_id))) return false;
    if (r->operation == 1) {
        // Non-sending ABI probe deliberately uses a nonexistent contact.
        return strcmp(r->recipient, "wab_probe_nonexistent_contact") == 0;
    }
    return r->recipient[0] && r->text[0] && r->request_id[0] &&
        strcmp(r->recipient, r->allowed_recipient) == 0;
}
static int Capture(EXCEPTION_POINTERS* fault, Request* r);
struct NativeShared { void* value; void* control; };
struct Location { const char* file; const char* function; uint32_t line; uint32_t pad; void* address; };
static_assert(sizeof(std::function<void()>) == 64);
struct Work {
    Request request;
    unsigned char* module;
    Callback* observed;
    Callback* transferred;
    ~Work() {
        if (transferred) Release(transferred);
        Release(observed);
    }
};
static void AssignString(unsigned char* module, String* target, const char* value) {
    const size_t length = strlen(value);
    if (length > target->capacity) {
        using Grow = void (*)(String*, size_t, size_t, const char*);
        reinterpret_cast<Grow>(module + 0x3e150)(target, length, 0, value);
    } else {
        char* destination = target->capacity < 16 ? target->data.small : const_cast<char*>(target->data.ptr);
        memcpy(destination, value, length + 1);
        target->size = length;
    }
}
static void DispatchFile(Work* work) {
    auto module = work->module;
    using ReleaseShared = void (*)(NativeShared*);
    auto release = reinterpret_cast<ReleaseShared>(module + 0x2060);
    NativeShared source{}, app{}, container{}, manager{};
    String path = Borrow(work->request.text), extension = Borrow("bin");
    using BuildFile = NativeShared* (*)(NativeShared*, const String*, const String*);
    // This builder only constructs a local file source. It never submits it.
    reinterpret_cast<BuildFile>(module + 0x59c8970)(&source, &path, &extension);
    // The builder defaults to self: replace it before the only sending call.
    AssignString(module, reinterpret_cast<String*>(static_cast<unsigned char*>(source.value) + 0xb0), work->request.recipient);
    // Preserve extensionless and Unicode names instead of the builder's .bin fallback.
    const char* filename = work->request.text;
    for (const char* cursor = filename; *cursor; ++cursor)
        if (*cursor == '/' || *cursor == '\\') filename = cursor + 1;
    AssignString(module, reinterpret_cast<String*>(static_cast<unsigned char*>(source.value) + 0x1a8), filename);
    using GetApp = NativeShared* (*)(NativeShared*);
    using GetService = NativeShared* (*)(void*, NativeShared*);
    reinterpret_cast<GetApp>(module + 0x47220)(&app);
    reinterpret_cast<GetService>(module + 0x3823c0)(app.value, &container);
    reinterpret_cast<GetService>(module + 0x708500)(container.value, &manager);
    struct SendResult { uint32_t category, code; String message; NativeShared detail; } result{};
    static_assert(sizeof(SendResult) == 56);
    using SendFile = SendResult* (*)(void*, SendResult*, NativeShared*);
    // Native ABI takes the shared source by value and releases this reference.
    reinterpret_cast<SendFile>(module + 0x179fea0)(manager.value, &result, &source);
    source = {};
    work->observed->status = (result.category == 0 && result.code == 0) ? 1 : 2;
    if (work->observed->status == 2)
        _snprintf_s(work->observed->error, sizeof(work->observed->error), _TRUNCATE,
                    "native file rejected: category=%u code=%u", result.category, result.code);
    release(&result.detail);
    if (result.message.capacity >= 16) {
        auto allocation = const_cast<char*>(result.message.data.ptr);
        size_t bytes = result.message.capacity + 1;
        if (bytes >= 4096) { allocation = reinterpret_cast<char**>(allocation)[-1]; bytes += 39; }
        using Free = void (*)(void*, size_t);
        reinterpret_cast<Free>(module + 0x7528b70)(allocation, bytes);
    }
    release(&manager); release(&container); release(&app);
    SetEvent(work->observed->event);
}
static void DispatchWork(Work* work) {
    __try {
        if (work->request.operation == 3) { DispatchFile(work); return; }
        TextParams parameters{Borrow(work->request.recipient), Borrow(work->request.text)};
        String request_id = Borrow(work->request.request_id);
        using Send = void (*)(void*, const TextParams*, const String*, Callback**);
        reinterpret_cast<Send>(work->module + 0x55c4580)(nullptr, &parameters, &request_id, &work->transferred);
    } __except(Capture(GetExceptionInformation(), &work->request)) {
        work->observed->status = 5;
        memcpy(work->observed->error, work->request.error, sizeof(work->observed->error));
        SetEvent(work->observed->event);
    }
}
static DWORD ExecuteImpl(Request* r) {
    if (!Validate(r)) return 10;
    r->status = 0;
    r->error[0] = 0;
    auto module = reinterpret_cast<unsigned char*>(GetModuleHandleW(L"Weixin.dll"));
    if (!module) return 11;
    // Full file hash is checked by the controller; verify the loaded entry too.
    const unsigned char prologue[] = {0x55,0x41,0x57,0x41,0x56,0x41,0x55,0x41,0x54,0x56,0x57,0x53,0x48,0x81,0xec,0x18,0x03,0x00,0x00};
    constexpr uintptr_t text_entry = 0x55c4580;
    if (memcmp(module + text_entry, prologue, sizeof(prologue))) return 12;
    auto* callback = static_cast<Callback*>(HeapAlloc(GetProcessHeap(), HEAP_ZERO_MEMORY, sizeof(Callback)));
    if (!callback) return 13;
    callback->refs = 3; // waiter, queued Work, and native once-callback ownership
    callback->invoke = Receive;
    callback->destroy = Destroy;
    callback->event = CreateEventW(nullptr, TRUE, FALSE, nullptr);
    if (!callback->event) { HeapFree(GetProcessHeap(), 0, callback); return 14; }
    auto work = std::make_shared<Work>();
    work->request = *r;
    work->module = module;
    work->observed = work->transferred = callback;
    // Service lookups require WeChat's managed task context (TLS +0x1f0).
    // Enter through its executor; do not copy TLS or patch the lookup function.
    auto global = *reinterpret_cast<void**>(module + 0xbb65e10);
    if (!global) { Release(callback); return 15; }
    NativeShared executor{}, task{};
    using GetExecutor = NativeShared* (*)(void*, NativeShared*);
    using ReleaseShared = void (*)(NativeShared*);
    reinterpret_cast<GetExecutor>(module + 0x47310)(global, &executor);
    if (!executor.value) {
        reinterpret_cast<void (*)(NativeShared*)>(module + 0x2060)(&executor);
        Release(callback); return 16;
    }
    std::function<void()> function = [work] { DispatchWork(work.get()); };
    Location location{"wechat-agent-bridge/native_text.cpp", "WabDispatch", 1, 0, nullptr};
    using Post = NativeShared* (*)(void*, NativeShared*, const Location*, std::function<void()>*, int);
    reinterpret_cast<Post>(module + 0x30f440)(executor.value, &task, &location, &function, 1);
    auto release = reinterpret_cast<ReleaseShared>(module + 0x2060);
    release(&task);
    release(&executor);
    work.reset();
    DWORD waited = WaitForSingleObject(callback->event, 30000);
    r->status = waited == WAIT_OBJECT_0 ? callback->status : 3;
    if (waited == WAIT_OBJECT_0) memcpy(r->error, callback->error, sizeof(r->error));
    Release(callback);
    return 0;
}
static int Capture(EXCEPTION_POINTERS* fault, Request* r) {
    if (r && r->magic == 0x57414232) {
        r->reserved = fault->ExceptionRecord->ExceptionCode;
        auto base = reinterpret_cast<uintptr_t>(GetModuleHandleW(L"Weixin.dll"));
        auto* c = fault->ContextRecord;
        _snprintf_s(r->error, sizeof(r->error), _TRUNCATE,
            "exception=%08lx rip_rva=%llx rcx=%llx rdx=%llx r8=%llx r9=%llx access=%llu address=%llx",
            fault->ExceptionRecord->ExceptionCode, c->Rip-base, c->Rcx, c->Rdx, c->R8, c->R9,
            fault->ExceptionRecord->ExceptionInformation[0], fault->ExceptionRecord->ExceptionInformation[1]);
    }
    return EXCEPTION_EXECUTE_HANDLER;
}
extern "C" __declspec(dllexport) DWORD WINAPI WabExecute(void* data) {
    __try { return ExecuteImpl(static_cast<Request*>(data)); }
    __except(Capture(GetExceptionInformation(), static_cast<Request*>(data))) {
        // Unknown outcome: never retry an interrupted native send automatically.
        return 20;
    }
}
BOOL WINAPI DllMain(HINSTANCE instance, DWORD reason, LPVOID) {
    if (reason == DLL_PROCESS_ATTACH) DisableThreadLibraryCalls(instance);
    return TRUE;
}
