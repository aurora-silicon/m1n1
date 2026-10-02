// SPDX-License-Identifier: MIT
use crate::println;
use alloc::boxed::Box;
use core::cmp::min;
use core::ffi::c_void;
use fatfs::SeekFrom;

extern "C" {
    pub(crate) fn nvme_read(nsid: u32, lba: u64, buffer: *mut c_void) -> bool;
    fn nvme_read_blocks(nsid: u32, lba: u64, buffer: *mut c_void, count: u32) -> bool;
}

const SECTOR_SIZE: usize = 4096;

pub type Error = ();

#[repr(C, align(4096))]
pub(crate) struct SectorBuffer(pub(crate) [u8; SECTOR_SIZE]);

pub(crate) fn alloc_sector_buf() -> Box<SectorBuffer> {
    let p: Box<SectorBuffer> = unsafe { Box::new_zeroed().assume_init() };
    debug_assert_eq!(0, p.0.as_ptr().align_offset(4096));
    p
}

/// Blocks fetched by one read-ahead command; must match NVME_MAX_READ_BLOCKS in nvme.h.
const WINDOW_SECTORS: usize = 64;

#[repr(C, align(4096))]
struct WindowBuffer([u8; SECTOR_SIZE * WINDOW_SECTORS]);

struct Window {
    start: u64,
    sectors: u64,
    buf: Box<WindowBuffer>,
}

impl Window {
    fn new() -> Window {
        let buf: Box<WindowBuffer> = unsafe { Box::new_zeroed().assume_init() };
        debug_assert_eq!(0, buf.0.as_ptr().align_offset(4096));
        Window {
            start: 0,
            sectors: 0,
            buf,
        }
    }

    fn contains(&self, lba: u64) -> bool {
        lba >= self.start && lba < self.start + self.sectors
    }
}

/// FAT reads alternate between the allocation table and the file data, so keep two windows:
/// a sequential run is fetched 64 blocks at a time while the table block stays cached.
pub struct NVMEStorage {
    nsid: u32,
    offset: u64,
    windows: [Window; 2],
    mru: usize,
    last_miss: Option<u64>,
    multi_ok: bool,
    pos: u64,
}

impl NVMEStorage {
    pub fn new(nsid: u32, offset: u64) -> NVMEStorage {
        NVMEStorage {
            nsid: nsid,
            offset: offset,
            windows: [Window::new(), Window::new()],
            mru: 0,
            last_miss: None,
            multi_ok: true,
            pos: 0,
        }
    }

    /// Returns the window index holding `lba` (relative to the partition), loading it if needed.
    fn fetch(&mut self, lba: u64) -> Result<usize, Error> {
        for i in 0..2 {
            if self.windows[i].contains(lba) {
                self.mru = i;
                return Ok(i);
            }
        }

        let victim = 1 - self.mru;
        let continues = |w: &Window| w.sectors > 0 && lba == w.start + w.sectors;
        let sequential = self.last_miss.map(|l| l + 1) == Some(lba)
            || continues(&self.windows[0])
            || continues(&self.windows[1]);
        self.last_miss = Some(lba);

        let abs = lba + self.offset;
        let w = &mut self.windows[victim];
        w.sectors = 0;
        let ptr = w.buf.0.as_mut_ptr() as *mut c_void;
        let mut count = if sequential && self.multi_ok { WINDOW_SECTORS as u32 } else { 1 };
        if count > 1 && !unsafe { nvme_read_blocks(self.nsid, abs, ptr, count) } {
            println!("nvme_read_blocks({}, {}, {}) failed, using single-block reads", self.nsid, abs, count);
            self.multi_ok = false;
            count = 1;
        }
        if count == 1 && !unsafe { nvme_read(self.nsid, abs, ptr) } {
            println!("nvme_read({}, {}) failed", self.nsid, abs);
            return Err(());
        }
        w.start = lba;
        w.sectors = count as u64;
        self.mru = victim;
        Ok(victim)
    }
}

impl fatfs::IoBase for NVMEStorage {
    type Error = Error;
}

impl fatfs::Read for NVMEStorage {
    fn read(&mut self, mut buf: &mut [u8]) -> Result<usize, Self::Error> {
        let mut read = 0;

        while !buf.is_empty() {
            let lba = self.pos / SECTOR_SIZE as u64;
            let i = self.fetch(lba)?;
            let w = &self.windows[i];
            let off = (self.pos - w.start * SECTOR_SIZE as u64) as usize;
            let avail = w.sectors as usize * SECTOR_SIZE - off;
            let copy_len = min(avail, buf.len());
            buf[..copy_len].copy_from_slice(&w.buf.0[off..off + copy_len]);
            buf = &mut buf[copy_len..];
            read += copy_len;
            self.pos += copy_len as u64;
        }
        Ok(read)
    }
}

impl fatfs::Write for NVMEStorage {
    fn write(&mut self, _buf: &[u8]) -> Result<usize, Self::Error> {
        Err(())
    }
    fn flush(&mut self) -> Result<(), Self::Error> {
        Err(())
    }
}

impl fatfs::Seek for NVMEStorage {
    fn seek(&mut self, from: SeekFrom) -> Result<u64, Self::Error> {
        self.pos = match from {
            SeekFrom::Start(n) => n,
            SeekFrom::End(_n) => panic!("SeekFrom::End not supported"),
            SeekFrom::Current(n) => self.pos.checked_add_signed(n).ok_or(())?,
        };
        Ok(self.pos)
    }
}
