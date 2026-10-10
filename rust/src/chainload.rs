// SPDX-License-Identifier: MIT
#![deny(unsafe_op_in_unsafe_fn)]

use crate::c_size_t;
use crate::gpt;
use crate::nvme;
use crate::println;
use alloc::boxed::Box;
use alloc::vec::Vec;
use core::ffi::{c_char, c_int, c_void, CStr};
use fatfs::{FileSystem, FsOptions, Read};
use uuid::Uuid;

#[derive(Debug)]
pub enum Error {
    FATError(fatfs::Error<nvme::Error>),
    GPTError(gpt::Error<nvme::Error>),
    BadArgs,
    PartitionNotFound,
    FileNotFound,
    AmbiguousFile,
    InvalidFile,
    OutOfMemory,
    UnexpectedEof,
}

impl From<fatfs::Error<nvme::Error>> for Error {
    fn from(err: fatfs::Error<nvme::Error>) -> Error {
        Error::FATError(err)
    }
}

fn valid_fat_path(path: &str) -> bool {
    !path.is_empty()
        && path
            .bytes()
            .all(|byte| (0x20..0x7f).contains(&byte) && byte != b';' && byte != b'\\')
        && path
            .split('/')
            .all(|component| !component.is_empty() && component != "." && component != "..")
}

#[cfg(test)]
mod path_tests {
    use super::valid_fat_path;

    #[test]
    fn stage1_path_grammar() {
        assert!(valid_fat_path("aurora/stage2.bin"));
        for path in [
            "aurora/a\0b",
            "aurora/a\x1fb",
            "aurora//b",
            "aurora/./b",
            "aurora/../b",
        ] {
            assert!(!valid_fat_path(path), "accepted {path:?}");
        }
    }
}

impl From<gpt::Error<nvme::Error>> for Error {
    fn from(err: gpt::Error<nvme::Error>) -> Error {
        Error::GPTError(err)
    }
}

fn load_image(spec: &str) -> Result<Vec<u8>, Error> {
    println!("Chainloading from the ESP");

    let mut args = spec.split(';');

    let uuid = Uuid::parse_str(args.next().ok_or(Error::BadArgs)?).or(Err(Error::BadArgs))?;
    let path = args.next().ok_or(Error::BadArgs)?;
    if args.next().is_some() || !valid_fat_path(path) {
        return Err(Error::BadArgs);
    }

    let part = {
        let storage = nvme::NVMEStorage::new(1, 0);
        let mut pt = gpt::GPT::new(storage)?;

        println!("Searching for the requested partition");
        pt.find_by_partuuid(uuid)?.ok_or(Error::PartitionNotFound)?
    };

    let offset = part.get_starting_lba();
    let sectors = part
        .get_ending_lba()
        .checked_sub(offset)
        .and_then(|n| n.checked_add(1))
        .ok_or(Error::BadArgs)?;

    println!("Partition offset: {}", offset);

    let storage = nvme::NVMEStorage::new(1, offset)
        .with_extent(sectors)
        .map_err(|_| Error::BadArgs)?
        .with_read_budget(512 * 1024 * 1024 / 4096);
    let opts = FsOptions::new().update_accessed_date(false);

    let fs = FileSystem::new(storage, opts)?;
    let (parent, file_name) = path.rsplit_once('/').unwrap_or(("", path));
    let root = fs.root_dir();
    let dir = if parent.is_empty() {
        root
    } else {
        root.open_dir(parent)?
    };
    let mut matched = None;
    for result in dir.iter() {
        let entry = result?;
        if entry.eq_name(file_name) {
            if matched.is_some() {
                return Err(Error::AmbiguousFile);
            }
            matched = Some(entry);
        }
    }
    let entry = matched.ok_or(Error::FileNotFound)?;
    if !entry.is_file() {
        return Err(Error::InvalidFile);
    }
    let size = entry.len() as usize;
    if size == 0 || size > 256 * 1024 * 1024 {
        return Err(Error::InvalidFile);
    }
    let mut file = entry.to_file();

    println!("File size: {}", size);

    let mut buf = Vec::new();
    buf.try_reserve_exact(size)
        .map_err(|_| Error::OutOfMemory)?;
    buf.resize(size, 0);
    let mut slice = &mut buf[..];
    while !slice.is_empty() {
        let read = file.read(slice)?;
        if read == 0 {
            return Err(Error::UnexpectedEof);
        }
        slice = &mut slice[read..];
    }
    println!("File read successfully");

    Ok(buf)
}

#[no_mangle]
pub unsafe extern "C" fn rust_load_image(
    raw_spec: *const c_char,
    image: *mut *mut c_void,
    size: *mut c_size_t,
) -> c_int {
    if raw_spec.is_null() || image.is_null() || size.is_null() {
        return -1;
    }
    let Ok(spec) = (unsafe { CStr::from_ptr(raw_spec).to_str() }) else {
        return -1;
    };

    match load_image(spec) {
        Ok(buf) => {
            let buf = Box::leak(buf.into_boxed_slice());
            unsafe {
                *size = buf.len();
                *image = buf.as_mut_ptr() as *mut c_void;
            }
            0
        }
        Err(err) => {
            println!("Chainload failed: {:?}", err);
            -1
        }
    }
}

#[no_mangle]
pub unsafe extern "C" fn rust_free_image(image: *mut c_void, size: c_size_t) {
    if !image.is_null() {
        let slice = core::ptr::slice_from_raw_parts_mut(image as *mut u8, size);
        drop(unsafe { Box::from_raw(slice) });
    }
}
