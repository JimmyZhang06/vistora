"""Native ownership for a child process tree; never attaches existing user processes."""

from __future__ import annotations


class WindowsProcessJob:
    """Own one child tree with JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE.

    The anonymous handle is not inheritable. Windows closes it on an API hard
    crash and kills the assigned worker and descendants. No persisted PID is
    trusted for crash recovery. See Microsoft Learn /windows/win32/procthread/job-objects.
    """

    def __init__(self) -> None:
        import ctypes
        from ctypes import wintypes

        class BasicLimits(ctypes.Structure):
            _fields_ = [
                ("process_time", ctypes.c_int64),
                ("job_time", ctypes.c_int64),
                ("flags", wintypes.DWORD),
                ("minimum_working_set", ctypes.c_size_t),
                ("maximum_working_set", ctypes.c_size_t),
                ("active_processes", wintypes.DWORD),
                ("affinity", ctypes.c_size_t),
                ("priority", wintypes.DWORD),
                ("scheduling_class", wintypes.DWORD),
            ]

        class IoCounters(ctypes.Structure):
            _fields_ = [
                (name, ctypes.c_uint64)
                for name in ("read_ops", "write_ops", "other_ops", "read", "write", "other")
            ]

        class ExtendedLimits(ctypes.Structure):
            _fields_ = [
                ("basic", BasicLimits),
                ("io", IoCounters),
                ("process_memory", ctypes.c_size_t),
                ("job_memory", ctypes.c_size_t),
                ("peak_process_memory", ctypes.c_size_t),
                ("peak_job_memory", ctypes.c_size_t),
            ]

        self._kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self._kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        self._kernel.CreateJobObjectW.restype = wintypes.HANDLE
        self._kernel.SetInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
        ]
        self._kernel.SetInformationJobObject.restype = wintypes.BOOL
        self._kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        self._kernel.AssignProcessToJobObject.restype = wintypes.BOOL
        self._kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        self._kernel.OpenProcess.restype = wintypes.HANDLE
        self._kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self._kernel.CloseHandle.restype = wintypes.BOOL
        self._handle = self._kernel.CreateJobObjectW(None, None)
        if not self._handle:
            raise OSError("Windows worker Job Object could not be created")
        limits = ExtendedLimits()
        limits.basic.flags = 0x00002000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not self._kernel.SetInformationJobObject(
            self._handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)
        ):
            self.close()
            raise OSError("Windows worker Job Object limits could not be set")

    def attach(self, process_id: int) -> None:
        # SET_QUOTA | TERMINATE, the rights required by AssignProcessToJobObject.
        process_handle = self._kernel.OpenProcess(0x0101, False, process_id)
        if not process_handle:
            raise OSError("Windows worker process could not be attached")
        try:
            if not self._kernel.AssignProcessToJobObject(self._handle, process_handle):
                raise OSError("Windows worker process could not be assigned to its Job Object")
        finally:
            self._kernel.CloseHandle(process_handle)

    def close(self) -> None:
        if self._handle:
            self._kernel.CloseHandle(self._handle)
            self._handle = None
