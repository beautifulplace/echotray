//! echotray-helperd - privileged paste daemon for EchoTray.
//!
//! Runs as root (via systemd). Owns the ONE privileged action EchoTray needs:
//! injecting Ctrl+V via /dev/uinput to paste the transcript at the cursor.
//!
//! There is no hotkey and no keyboard reading. The trigger is the tray icon in
//! the GUI: the user clicks it to start/stop recording, and the GUI sends a
//! `paste` request here when it wants the text inserted.
//!
//! The GUI runs as an unprivileged user with NO special groups and talks to this
//! daemon over a Unix socket at /run/echotray.sock. Peers are authorized by
//! real UID (SO_PEERCRED): only non-root users may connect. This keeps
//! /dev/uinput out of unprivileged hands.
//!
//! Written in Rust with ZERO external crates (pure std + hand-rolled libc FFI)
//! so it builds fully offline with a minimal, auditable supply-chain surface.
//!
//! Protocol (newline-delimited JSON messages):
//!   GUI -> daemon:  {"cmd":"paste"}

#![allow(dead_code)]

use std::ffi::c_int;
use std::os::unix::io::RawFd;

// ─────────────────────────────── libc FFI ────────────────────────────────────

#[repr(C)]
#[derive(Clone, Copy)]
struct input_event {
    time: [i64; 2], // struct timeval { tv_sec, tv_usec } on 64-bit
    type_: u16,
    code: u16,
    value: i32,
}

#[repr(C)]
#[derive(Clone, Copy)]
struct input_id {
    bustype: u16,
    vendor: u16,
    product: u16,
    version: u16,
}

#[repr(C)]
#[derive(Clone, Copy)]
struct uinput_setup {
    id: input_id,
    name: [u8; 80],
    ff_effects_max: u32,
}

#[repr(C)]
#[derive(Clone, Copy)]
struct ucred {
    pid: i32,
    uid: u32,
    gid: u32,
}

#[repr(C)]
struct sockaddr_un {
    sun_family: u16,
    sun_path: [u8; 108],
}

// Constants (Linux input subsystem)
const EV_KEY: u16 = 0x01;
const EV_SYN: u16 = 0x00;
const SYN_REPORT: u16 = 0;
const KEY_ESC: u16 = 1;
const KEY_MICMUTE: u16 = 248;
const KEY_LEFTCTRL: u16 = 29;
const KEY_V: u16 = 47;

// uinput ioctl codes (computed from _IOC; verified against kernel headers)
const UI_SET_EVBIT: u64 = 0x40045564; // _IOW('U',100,int)
const UI_SET_KEYBIT: u64 = 0x40045565; // _IOW('U',101,int)
const UI_DEV_SETUP: u64 = 0x405c5503; // _IOW('U',3,uinput_setup) - size 92
const UI_DEV_CREATE: u64 = 0x5501; // _IO('U',1)
const UI_DEV_DESTROY: u64 = 0x5502; // _IO('U',2)

// poll
const POLLIN: i16 = 0x0001;
const POLLHUP: i16 = 0x0010;
const POLLERR: i16 = 0x0008;

// socket / SO_PEERCRED
const AF_UNIX: c_int = 1;
const SOCK_STREAM: c_int = 1;
const SOL_SOCKET: c_int = 1;
const SO_PEERCRED: c_int = 17;

// fcntl flags
const O_WRONLY: c_int = 1;
const O_NONBLOCK: c_int = 0o4000;

const SOCK_PATH: &str = "/run/echotray.sock";
const MAX_CLIENTS: usize = 8;

extern "C" {
    fn socket(domain: c_int, ty: c_int, protocol: c_int) -> c_int;
    fn bind(fd: c_int, addr: *const sockaddr_un, len: u32) -> c_int;
    fn listen(fd: c_int, backlog: c_int) -> c_int;
    fn accept(fd: c_int, addr: *mut sockaddr_un, len: *mut u32) -> c_int;
    fn unlink(path: *const u8) -> c_int;
    fn chmod(path: *const u8, mode: u32) -> c_int;
    fn close(fd: c_int) -> c_int;
    fn read(fd: c_int, buf: *mut u8, count: usize) -> isize;
    fn write(fd: c_int, buf: *const u8, count: usize) -> isize;
    fn getsockopt(fd: c_int, level: c_int, optname: c_int, optval: *mut u8, optlen: *mut u32) -> c_int;
    fn poll(fds: *mut PollFd, nfds: usize, timeout: c_int) -> c_int;
    fn open(path: *const u8, flags: c_int) -> c_int;
    fn ioctl(fd: c_int, request: u64, ...) -> c_int;
    fn usleep(usec: u32) -> c_int;
    fn strerror(errnum: c_int) -> *const u8;
}

#[repr(C)]
#[derive(Clone, Copy)]
struct PollFd {
    fd: c_int,
    events: i16,
    revents: i16,
}

// ─────────────────────────── helpers ─────────────────────────────────────────

fn errno() -> c_int {
    std::io::Error::last_os_error().raw_os_error().unwrap_or(0)
}

fn strerror_str(err: c_int) -> String {
    unsafe {
        let p = strerror(err);
        if p.is_null() {
            format!("errno {err}")
        } else {
            let mut len = 0usize;
            while *p.add(len) != 0 {
                len += 1;
            }
            let slice = std::slice::from_raw_parts(p, len);
            String::from_utf8_lossy(slice).into_owned()
        }
    }
}

/// NUL-terminated path bytes for C calls (unlink, chmod).
fn c_path_bytes(path: &str) -> Vec<u8> {
    let mut v = path.as_bytes().to_vec();
    v.push(0);
    v
}

// ─────────────────────────── uinput (Ctrl+V) ─────────────────────────────────

struct UInput {
    fd: RawFd,
}

impl UInput {
    fn open() -> Result<Self, String> {
        let path = b"/dev/uinput\0";
        let fd = unsafe { open(path.as_ptr(), O_WRONLY | O_NONBLOCK) };
        if fd < 0 {
            return Err(format!("open /dev/uinput: {}", strerror_str(errno())));
        }

        let r = unsafe { ioctl(fd, UI_SET_EVBIT, EV_KEY as c_int) };
        if r < 0 {
            unsafe { close(fd) };
            return Err(format!("UI_SET_EVBIT: {}", strerror_str(errno())));
        }
        // We only need to inject Ctrl+V, but enable a small range to be safe.
        for k in KEY_ESC..=KEY_MICMUTE {
            unsafe { ioctl(fd, UI_SET_KEYBIT, k as c_int) };
        }

        let mut setup = uinput_setup {
            id: input_id {
                bustype: 3, // BUS_USB
                vendor: 0x1234,
                product: 0x5678,
                version: 1,
            },
            name: [0u8; 80],
            ff_effects_max: 0,
        };
        let name = b"EchoTray Virtual Keyboard\0";
        let n = name.len().min(79);
        setup.name[..n].copy_from_slice(&name[..n]);

        let r = unsafe { ioctl(fd, UI_DEV_SETUP, &setup) };
        if r < 0 {
            unsafe { close(fd) };
            return Err(format!("UI_DEV_SETUP: {}", strerror_str(errno())));
        }
        let r = unsafe { ioctl(fd, UI_DEV_CREATE, 0) };
        if r < 0 {
            unsafe { close(fd) };
            return Err(format!("UI_DEV_CREATE: {}", strerror_str(errno())));
        }

        Ok(UInput { fd })
    }

    fn write_key(&self, code: u16, value: i32) {
        let ev = input_event {
            time: [0, 0],
            type_: EV_KEY,
            code,
            value,
        };
        unsafe {
            write(self.fd, &ev as *const _ as *const u8, std::mem::size_of::<input_event>());
        }
    }

    fn write_syn(&self) {
        let ev = input_event {
            time: [0, 0],
            type_: EV_SYN,
            code: SYN_REPORT,
            value: 0,
        };
        unsafe {
            write(self.fd, &ev as *const _ as *const u8, std::mem::size_of::<input_event>());
        }
    }

    fn inject_ctrl_v(&self) {
        self.write_key(KEY_LEFTCTRL, 1);
        self.write_key(KEY_V, 1);
        self.write_syn();
        unsafe { usleep(50000) };
        self.write_key(KEY_V, 0);
        self.write_key(KEY_LEFTCTRL, 0);
        self.write_syn();
    }
}

impl Drop for UInput {
    fn drop(&mut self) {
        unsafe {
            ioctl(self.fd, UI_DEV_DESTROY, 0);
            close(self.fd);
        }
    }
}

// ─────────────────────────── socket server ───────────────────────────────────

fn open_listen_socket() -> Result<RawFd, String> {
    unsafe { unlink(c_path_bytes(SOCK_PATH).as_ptr()) };
    let fd = unsafe { socket(AF_UNIX, SOCK_STREAM, 0) };
    if fd < 0 {
        return Err(format!("socket: {}", strerror_str(errno())));
    }

    let mut addr = sockaddr_un {
        sun_family: AF_UNIX as u16,
        sun_path: [0u8; 108],
    };
    let bytes = SOCK_PATH.as_bytes();
    if bytes.len() >= 108 {
        unsafe { close(fd) };
        return Err("socket path too long".to_string());
    }
    addr.sun_path[..bytes.len()].copy_from_slice(bytes);

    let r = unsafe {
        bind(
            fd,
            &addr as *const sockaddr_un,
            std::mem::size_of::<sockaddr_un>() as u32,
        )
    };
    if r < 0 {
        unsafe { close(fd) };
        return Err(format!("bind: {}", strerror_str(errno())));
    }
    let r = unsafe { listen(fd, 8) };
    if r < 0 {
        unsafe { close(fd) };
        return Err(format!("listen: {}", strerror_str(errno())));
    }
    // The socket file must be connectable by the unprivileged GUI user. Safe
    // because the path is root-only (in /run) and auth is SO_PEERCRED.
    unsafe {
        let path = c_path_bytes(SOCK_PATH);
        let _ = chmod(path.as_ptr(), 0o666);
    }
    Ok(fd)
}

fn peer_is_nonroot(fd: RawFd) -> bool {
    let mut cred = ucred { pid: 0, uid: 0, gid: 0 };
    let mut len = std::mem::size_of::<ucred>() as u32;
    let r = unsafe {
        getsockopt(
            fd,
            SOL_SOCKET,
            SO_PEERCRED,
            &mut cred as *mut ucred as *mut u8,
            &mut len,
        )
    };
    if r < 0 {
        return false;
    }
    cred.uid != 0
}

/// Read and handle one client connection's messages. Returns when the client
/// disconnects.
fn handle_client(cfd: RawFd, uinput: &Option<UInput>) {
    let mut buf: Vec<u8> = Vec::new();
    loop {
        // Read until we have a full newline-terminated message or EOF.
        let mut got = false;
        while !buf.contains(&b'\n') {
            let mut chunk = [0u8; 4096];
            let n = unsafe { read(cfd, chunk.as_mut_ptr(), chunk.len()) };
            if n <= 0 {
                return; // closed or error
            }
            buf.extend_from_slice(&chunk[..n as usize]);
            got = true;
            if buf.len() > 8192 {
                buf.clear(); // defensive: drop oversized input
            }
        }

        // Process all complete lines in the buffer.
        loop {
            match buf.iter().position(|&b| b == b'\n') {
                None => break,
                Some(nl) => {
                    let line: Vec<u8> = buf[..nl].to_vec();
                    buf.drain(..=nl);
                    let text = String::from_utf8_lossy(&line);
                    if text.contains("\"paste\"") {
                        if let Some(u) = uinput {
                            u.inject_ctrl_v();
                        }
                    }
                }
            }
        }

        if !got {
            // No new data; loop again to read more.
        }
    }
}

// ─────────────────────────── main ────────────────────────────────────────────

fn main() {
    let listen_fd = match open_listen_socket() {
        Ok(fd) => fd,
        Err(e) => {
            eprintln!("echotray-helperd: {e}");
            std::process::exit(1);
        }
    };

    let uinput = match UInput::open() {
        Ok(u) => Some(u),
        Err(e) => {
            eprintln!("echotray-helperd: {e}; Ctrl+V injection disabled");
            None
        }
    };

    let mut clients: Vec<RawFd> = vec![-1; MAX_CLIENTS];

    eprintln!(
        "echotray-helperd: ready ({}) - waiting for paste requests",
        if uinput.is_some() { "uinput attached" } else { "uinput unavailable" }
    );

    loop {
        let n = 1 + MAX_CLIENTS;
        let mut pfds: Vec<PollFd> = Vec::with_capacity(n);

        pfds.push(PollFd {
            fd: listen_fd,
            events: POLLIN,
            revents: 0,
        });
        for &cfd in &clients {
            if cfd >= 0 {
                pfds.push(PollFd {
                    fd: cfd,
                    events: POLLIN,
                    revents: 0,
                });
            } else {
                pfds.push(PollFd {
                    fd: -1,
                    events: 0,
                    revents: 0,
                });
            }
        }

        let rc = unsafe { poll(pfds.as_mut_ptr(), pfds.len(), 500) };
        if rc < 0 {
            if errno() == 4 {
                // EINTR
                continue;
            }
            eprintln!("echotray-helperd: poll: {}", strerror_str(errno()));
            break;
        }
        if rc == 0 {
            continue;
        }

        let mut idx = 0;

        // accept new connection
        if pfds[idx].revents & POLLIN != 0 {
            let cfd = unsafe { accept(listen_fd, std::ptr::null_mut(), std::ptr::null_mut()) };
            if cfd >= 0 {
                if peer_is_nonroot(cfd) {
                    let mut slot = None;
                    for (i, &c) in clients.iter().enumerate() {
                        if c < 0 {
                            slot = Some(i);
                            break;
                        }
                    }
                    match slot {
                        Some(i) => clients[i] = cfd,
                        None => {
                            let _ = unsafe { close(cfd) };
                        }
                    }
                } else {
                    let _ = unsafe { close(cfd) };
                }
            }
        }
        idx += 1;

        // handle client I/O
        for i in 0..MAX_CLIENTS {
            if clients[i] >= 0 && (pfds[idx].revents & (POLLIN | POLLHUP | POLLERR)) != 0 {
                handle_client(clients[i], &uinput);
                // handle_client returns on EOF/error; drop the connection.
                let _ = unsafe { close(clients[i]) };
                clients[i] = -1;
            }
            idx += 1;
        }
    }

    // cleanup
    for &cfd in &clients {
        if cfd >= 0 {
            let _ = unsafe { close(cfd) };
        }
    }
    let _ = unsafe { close(listen_fd) };
    let _ = unsafe { unlink(c_path_bytes(SOCK_PATH).as_ptr()) };
}
