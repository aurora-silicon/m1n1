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
const BULK_SECTORS: usize = 256;

pub type Error = ();

#[repr(C, align(4096))]
pub(crate) struct SectorBuffer(pub(crate) [u8; SECTOR_SIZE]);

#[repr(C, align(4096))]
struct BulkBuffer([u8; SECTOR_SIZE * BULK_SECTORS]);

pub(crate) fn alloc_sector_buf() -> Box<SectorBuffer> {
    let p: Box<SectorBuffer> = unsafe { Box::new_zeroed().assume_init() };
    debug_assert_eq!(0, p.0.as_ptr().align_offset(SECTOR_SIZE));
    p
}

pub struct NVMEStorage {
    nsid: u32,
    offset: u64,
    extent_sectors: Option<u64>,
    read_budget: Option<u64>,
    bytes_budget: Option<u64>,
    op_budget: Option<u64>,
    cached_lba: [Option<u64>; 2],
    cache: [Box<SectorBuffer>; 2],
    last_used: usize,
    bulk: Option<Box<BulkBuffer>>,
    pos: u64,
}

impl NVMEStorage {
    pub fn new(nsid: u32, offset: u64) -> NVMEStorage {
        NVMEStorage {
            nsid,
            offset,
            extent_sectors: None,
            read_budget: None,
            bytes_budget: None,
            op_budget: None,
            cached_lba: [None, None],
            cache: [alloc_sector_buf(), alloc_sector_buf()],
            last_used: 0,
            bulk: None,
            pos: 0,
        }
    }

    pub fn with_extent(mut self, sectors: u64) -> Result<Self, Error> {
        if sectors == 0
            || self.offset.checked_add(sectors - 1).is_none()
            || sectors.checked_mul(SECTOR_SIZE as u64).is_none()
        {
            return Err(());
        }
        self.extent_sectors = Some(sectors);
        Ok(self)
    }

    pub fn with_read_budget(mut self, sectors: u64) -> Self {
        self.read_budget = Some(sectors);
        self.bytes_budget = Some(sectors.saturating_mul(SECTOR_SIZE as u64));
        self.op_budget = Some((1 << 20) - 1);
        self
    }

    fn charge_op(&mut self) -> Result<(), Error> {
        if let Some(left) = self.op_budget.as_mut() {
            if *left == 0 {
                return Err(());
            }
            *left -= 1;
        }
        Ok(())
    }

    fn charge(&mut self, sectors: usize) -> Result<(), Error> {
        if let Some(left) = self.read_budget.as_mut() {
            if *left < sectors as u64 {
                return Err(());
            }
            *left -= sectors as u64;
        }
        Ok(())
    }

    fn available_sectors(&self, relative_lba: u64) -> u64 {
        self.extent_sectors
            .map(|extent| extent.saturating_sub(relative_lba))
            .unwrap_or(u64::MAX)
    }

    fn read_sector(&mut self, relative_lba: u64) -> Result<usize, Error> {
        if self.cached_lba[self.last_used] == Some(relative_lba) {
            return Ok(self.last_used);
        }
        let other = 1 - self.last_used;
        if self.cached_lba[other] == Some(relative_lba) {
            self.last_used = other;
            return Ok(other);
        }
        self.charge(1)?;
        let lba = self.offset.checked_add(relative_lba).ok_or(())?;
        self.cached_lba[other] = None;
        if !unsafe { nvme_read(self.nsid, lba, self.cache[other].0.as_mut_ptr().cast()) } {
            println!("nvme_read({}, {}) failed", self.nsid, lba);
            return Err(());
        }
        self.cached_lba[other] = Some(relative_lba);
        self.last_used = other;
        Ok(other)
    }
}

impl fatfs::IoBase for NVMEStorage {
    type Error = Error;
}

impl fatfs::Read for NVMEStorage {
    fn read(&mut self, mut buf: &mut [u8]) -> Result<usize, Self::Error> {
        self.charge_op()?;
        if let Some(left) = self.bytes_budget.as_mut() {
            if *left < buf.len() as u64 {
                return Err(());
            }
            *left -= buf.len() as u64;
        }
        let mut read = 0;
        while !buf.is_empty() {
            let relative_lba = self.pos / SECTOR_SIZE as u64;
            if self.available_sectors(relative_lba) == 0 {
                break;
            }
            let off = self.pos as usize % SECTOR_SIZE;
            let max_bytes = self
                .available_sectors(relative_lba)
                .saturating_mul(SECTOR_SIZE as u64)
                .saturating_sub(off as u64);
            let requested = min(buf.len(), min(max_bytes, usize::MAX as u64) as usize);
            if requested == 0 {
                break;
            }

            if requested > SECTOR_SIZE - off && self.available_sectors(relative_lba) > 1 {
                let sectors = min(
                    BULK_SECTORS,
                    min(
                        self.available_sectors(relative_lba),
                        (off.saturating_add(requested).saturating_add(SECTOR_SIZE - 1)
                            / SECTOR_SIZE) as u64,
                    ) as usize,
                );
                self.charge(sectors)?;
                let lba = self.offset.checked_add(relative_lba).ok_or(())?;
                if self.bulk.is_none() {
                    self.bulk = Some(unsafe { Box::new_zeroed().assume_init() });
                }
                let bulk = self.bulk.as_mut().ok_or(())?;
                if !unsafe {
                    nvme_read_blocks(self.nsid, lba, bulk.0.as_mut_ptr().cast(), sectors as u32)
                } {
                    return Err(());
                }
                let copy_len = min(requested, sectors * SECTOR_SIZE - off);
                buf[..copy_len].copy_from_slice(&bulk.0[off..off + copy_len]);
                buf = &mut buf[copy_len..];
                read += copy_len;
                self.pos = self.pos.checked_add(copy_len as u64).ok_or(())?;
                continue;
            }

            let slot = self.read_sector(relative_lba)?;
            let copy_len = min(SECTOR_SIZE - off, requested);
            buf[..copy_len].copy_from_slice(&self.cache[slot].0[off..off + copy_len]);
            buf = &mut buf[copy_len..];
            read += copy_len;
            self.pos = self.pos.checked_add(copy_len as u64).ok_or(())?;
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
        self.charge_op()?;
        self.pos = match from {
            SeekFrom::Start(n) => n,
            SeekFrom::End(_n) => return Err(()),
            SeekFrom::Current(n) => self.pos.checked_add_signed(n).ok_or(())?,
        };
        Ok(self.pos)
    }
}
