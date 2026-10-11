// Local debug only (DS2_CRASHDUMP): on an access violation, log the host stack as
// module RVAs and write a minidump next to the logs. Not part of the shipped build.
#include <windows.h>
#include <dbghelp.h>
#include <atomic>
#include <cstdio>
#pragma comment(lib, "dbghelp.lib")

namespace {
std::atomic<int> g_hits{0};

void Rva(FILE* f, const char* tag, DWORD64 pc) {
  HMODULE m = nullptr;
  char name[MAX_PATH] = "?";
  if (GetModuleHandleExA(GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS | GET_MODULE_HANDLE_EX_FLAG_UNCHANGED_REFCOUNT,
                         reinterpret_cast<LPCSTR>(pc), &m)) {
    GetModuleFileNameA(m, name, MAX_PATH);
    const char* b = strrchr(name, 92);
    fprintf(f, "%s %s+0x%llX\n", tag, b ? b + 1 : name, pc - reinterpret_cast<DWORD64>(m));
  } else {
    fprintf(f, "%s 0x%llX\n", tag, pc);
  }
}

LONG CALLBACK Last(PEXCEPTION_POINTERS ep) {
  if (ep->ExceptionRecord->ExceptionCode != EXCEPTION_ACCESS_VIOLATION) return EXCEPTION_CONTINUE_SEARCH;
  int n = g_hits.fetch_add(1);
  if (n >= 4) return EXCEPTION_CONTINUE_SEARCH;
  char path[64];
  sprintf_s(path, "ds2_crash_%d.txt", n);
  FILE* f = nullptr;
  fopen_s(&f, path, "w");
  if (f) {
    CONTEXT c = *ep->ContextRecord;
    fprintf(f, "tid %lu %s 0x%llX\n", GetCurrentThreadId(),
            ep->ExceptionRecord->ExceptionInformation[0] ? "write" : "read",
            (unsigned long long)ep->ExceptionRecord->ExceptionInformation[1]);
    const DWORD64* r = &c.Rax;
    static const char* rn[16] = {"rax","rcx","rdx","rbx","rsp","rbp","rsi","rdi","r8","r9","r10","r11","r12","r13","r14","r15"};
    for (int i = 0; i < 16; ++i) fprintf(f, "%s %016llX\n", rn[i], r[i]);
    for (int depth = 0; depth < 48 && c.Rip; ++depth) {
      Rva(f, "#", c.Rip);
      DWORD64 base = 0;
      PRUNTIME_FUNCTION fn = RtlLookupFunctionEntry(c.Rip, &base, nullptr);
      if (!fn) { c.Rip = *reinterpret_cast<DWORD64*>(c.Rsp); c.Rsp += 8; continue; }
      PVOID hd; DWORD64 est;
      RtlVirtualUnwind(UNW_FLAG_NHANDLER, base, c.Rip, fn, &c, &hd, &est, nullptr);
    }
    fclose(f);
  }
  sprintf_s(path, "ds2_crash_%d.dmp", n);
  HANDLE h = CreateFileA(path, GENERIC_WRITE, 0, nullptr, CREATE_ALWAYS, 0, nullptr);
  if (h != INVALID_HANDLE_VALUE) {
    MINIDUMP_EXCEPTION_INFORMATION mi{GetCurrentThreadId(), ep, FALSE};
    MiniDumpWriteDump(GetCurrentProcess(), GetCurrentProcessId(), h,
                      MINIDUMP_TYPE(MiniDumpWithIndirectlyReferencedMemory | MiniDumpWithThreadInfo), &mi, nullptr, nullptr);
    CloseHandle(h);
  }
  return EXCEPTION_CONTINUE_SEARCH;
}

struct Install {
  Install() { AddVectoredExceptionHandler(0, Last); }
} g_install;
}  // namespace
