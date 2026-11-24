use objc2::runtime::{AnyObject, Sel};
use objc2::{class, ffi, msg_send, sel};
use std::{io, mem, sync::OnceLock};
use tauri::AppHandle;

static APP: OnceLock<AppHandle> = OnceLock::new();

extern "C-unwind" fn should_terminate(_: &AnyObject, _: Sel, _: &AnyObject) -> usize {
    crate::backend::request_exit(APP.get().expect("native quit handler installed"));
    0
}

pub fn install_quit_handler(app: &AppHandle) -> io::Result<()> {
    let _ = APP.set(app.clone());
    // AppKit's native Quit skips Tauri's ExitRequested event.
    unsafe {
        let application: *const AnyObject = msg_send![class!(NSApplication), sharedApplication];
        let delegate: *const AnyObject = msg_send![application, delegate];
        let class = ffi::object_getClass(delegate).cast_mut();
        let method = mem::transmute::<extern "C-unwind" fn(&AnyObject, Sel, &AnyObject) -> usize, _>(should_terminate);
        if !class.is_null()
            && ffi::class_addMethod(class, sel!(applicationShouldTerminate:), method, c"Q@:@".as_ptr()).as_bool()
        {
            return Ok(());
        }
    }
    Err(io::Error::other("Could not install native quit handler"))
}
