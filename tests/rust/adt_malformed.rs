// SPDX-License-Identifier: MIT
// Host harness for the production Rust ADT parser.
#![allow(non_camel_case_types)]

use std::ffi::{c_void, CString};

type c_size_t = usize;

#[path = "../../rust/src/adt.rs"]
mod parser;

#[no_mangle]
static mut adt: *const c_void = std::ptr::null();
static mut ADT_SIZE: u32 = 0;

#[no_mangle]
extern "C" fn adt_get_size() -> u32 {
    unsafe { ADT_SIZE }
}

fn main() {
    let mut blob = vec![0u8; 344];
    blob[0..4].copy_from_slice(&1u32.to_le_bytes());
    blob[8..13].copy_from_slice(b"name\0");
    blob[40..44].copy_from_slice(&300u32.to_le_bytes());
    let name = CString::new("name").unwrap();
    let mut out = [0xa5u8; 300];

    unsafe {
        adt = blob.as_ptr().cast();
        ADT_SIZE = blob.len() as u32;

        assert_eq!(parser::adt_getprop_copy(adt, 0, name.as_ptr(), out.as_mut_ptr().cast(), 299), -20);
        assert!(out.iter().all(|&byte| byte == 0xa5));
        assert_eq!(parser::adt_getprop_copy(adt, 0, name.as_ptr(), out.as_mut_ptr().cast(), 300), 300);
        assert!(out.iter().all(|&byte| byte == 0));

        blob[40..44].copy_from_slice(&301u32.to_le_bytes());
        assert_eq!(parser::adt_getprop_copy(adt, 0, name.as_ptr(), out.as_mut_ptr().cast(), 300), -4);
        assert_eq!(parser::adt_getprop_copy(adt, -1, name.as_ptr(), out.as_mut_ptr().cast(), 300), -4);
        assert_eq!(parser::adt_first_child_offset(adt, -1), -4);
        assert_eq!(parser::adt_next_sibling_offset(adt, -1), -4);
    }
}
