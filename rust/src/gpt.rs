// SPDX-License-Identifier: MIT

use core::cmp::min;
use core::convert::TryInto;
use fatfs::{Read, Seek, SeekFrom};
use uuid::Uuid;

const EFI_SIGNATURE: u64 = 0x5452415020494645;
const SECTOR_SIZE: u64 = 4096;
const MAX_ENTRIES: usize = 4096;
const MAX_TABLE_BYTES: usize = 16 * 1024 * 1024;

#[derive(Debug)]
pub enum Error<T> {
    Io(T),
    InvalidGPTHeader,
    InvalidGPTEntry,
    DuplicatePartitionUuid,
    OutOfRange,
    Overflow,
}

impl<T> From<T> for Error<T> {
    fn from(err: T) -> Self {
        Error::Io(err)
    }
}

fn crc32_update(mut crc: u32, bytes: &[u8]) -> u32 {
    for &byte in bytes {
        crc ^= byte as u32;
        for _ in 0..8 {
            crc = (crc >> 1) ^ (0xedb8_8320 & (0u32.wrapping_sub(crc & 1)));
        }
    }
    crc
}

struct TableHeader {
    bytes: [u8; Self::SIZE],
}

impl TableHeader {
    const SIZE: usize = 92;

    fn u32_at(&self, off: usize) -> u32 {
        u32::from_le_bytes(self.bytes[off..off + 4].try_into().unwrap())
    }

    fn u64_at(&self, off: usize) -> u64 {
        u64::from_le_bytes(self.bytes[off..off + 8].try_into().unwrap())
    }

    fn read<R: Read + Seek>(rdr: &mut R) -> Result<Self, Error<R::Error>> {
        let mut hdr = Self {
            bytes: [0; Self::SIZE],
        };
        rdr.seek(SeekFrom::Start(SECTOR_SIZE))?;
        rdr.read_exact(&mut hdr.bytes)?;
        let expected_crc = hdr.u32_at(16);
        hdr.bytes[16..20].fill(0);
        let valid = hdr.u64_at(0) == EFI_SIGNATURE
            && hdr.u32_at(8) == 0x0001_0000
            && hdr.u32_at(12) == Self::SIZE as u32
            && hdr.u32_at(20) == 0
            && hdr.u64_at(24) == 1
            && !crc32_update(!0, &hdr.bytes) == expected_crc;
        hdr.bytes[16..20].copy_from_slice(&expected_crc.to_le_bytes());
        if !valid {
            return Err(Error::InvalidGPTHeader);
        }
        let first = hdr.u64_at(40);
        let last = hdr.u64_at(48);
        let alternate = hdr.u64_at(32);
        let count = hdr.u32_at(80) as usize;
        let entry_size = hdr.u32_at(84) as usize;
        if first > last
            || last >= alternate
            || count > MAX_ENTRIES
            || entry_size < 128
            || !entry_size.is_power_of_two()
        {
            return Err(Error::InvalidGPTHeader);
        }
        let table_bytes = count.checked_mul(entry_size).ok_or(Error::Overflow)?;
        if table_bytes > MAX_TABLE_BYTES || hdr.u64_at(72) < 2 {
            return Err(Error::InvalidGPTHeader);
        }
        let table_start = hdr
            .u64_at(72)
            .checked_mul(SECTOR_SIZE)
            .ok_or(Error::Overflow)?;
        let table_end = table_start
            .checked_add(table_bytes as u64)
            .ok_or(Error::Overflow)?;
        let usable_start = first.checked_mul(SECTOR_SIZE).ok_or(Error::Overflow)?;
        if table_end > usable_start {
            return Err(Error::InvalidGPTHeader);
        }
        Ok(hdr)
    }

    fn table_offset(&self) -> Result<u64, Error<()>> {
        self.u64_at(72)
            .checked_mul(SECTOR_SIZE)
            .ok_or(Error::Overflow)
    }

    fn count(&self) -> usize {
        self.u32_at(80) as usize
    }

    fn entry_size(&self) -> usize {
        self.u32_at(84) as usize
    }
}

pub struct PartitionEntry {
    bytes: [u8; Self::SIZE],
}

impl PartitionEntry {
    const SIZE: usize = 128;

    fn read<R: Read + Seek>(rdr: &mut R, off: u64) -> Result<Self, Error<R::Error>> {
        let mut part = Self {
            bytes: [0; Self::SIZE],
        };
        rdr.seek(SeekFrom::Start(off))?;
        rdr.read_exact(&mut part.bytes)?;
        Ok(part)
    }

    pub fn get_type_guid(&self) -> Uuid {
        Uuid::from_bytes_le(self.bytes[0..16].try_into().unwrap())
    }

    pub fn get_partition_guid(&self) -> Uuid {
        Uuid::from_bytes_le(self.bytes[16..32].try_into().unwrap())
    }

    pub fn get_starting_lba(&self) -> u64 {
        u64::from_le_bytes(self.bytes[32..40].try_into().unwrap())
    }

    pub fn get_ending_lba(&self) -> u64 {
        u64::from_le_bytes(self.bytes[40..48].try_into().unwrap())
    }
}

pub struct GPT<T: fatfs::ReadWriteSeek> {
    disk: T,
    hdr: TableHeader,
}

impl<IO: fatfs::ReadWriteSeek> GPT<IO> {
    pub fn new<T: fatfs::IntoStorage<IO>>(storage: T) -> Result<Self, Error<IO::Error>> {
        let mut disk = storage.into_storage();
        let hdr = TableHeader::read(&mut disk)?;
        let table_start = hdr.table_offset().map_err(|_| Error::Overflow)?;
        disk.seek(SeekFrom::Start(table_start))?;
        let mut remaining = hdr.count() * hdr.entry_size();
        let mut crc = !0u32;
        let mut block = [0u8; 4096];
        while remaining > 0 {
            let take = min(remaining, block.len());
            disk.read_exact(&mut block[..take])?;
            crc = crc32_update(crc, &block[..take]);
            remaining -= take;
        }
        if !crc != hdr.u32_at(88) {
            return Err(Error::InvalidGPTHeader);
        }
        Ok(Self { disk, hdr })
    }

    pub fn count(&self) -> usize {
        self.hdr.count()
    }

    pub fn index(&mut self, index: usize) -> Result<PartitionEntry, Error<IO::Error>> {
        if index >= self.count() {
            return Err(Error::OutOfRange);
        }
        let off = self
            .hdr
            .table_offset()
            .map_err(|_| Error::Overflow)?
            .checked_add((index * self.hdr.entry_size()) as u64)
            .ok_or(Error::Overflow)?;
        let part = PartitionEntry::read(&mut self.disk, off)?;
        if !part.get_type_guid().is_nil()
            && (part.get_partition_guid().is_nil()
                || part.get_starting_lba() < self.hdr.u64_at(40)
                || part.get_starting_lba() > part.get_ending_lba()
                || part.get_ending_lba() > self.hdr.u64_at(48))
        {
            return Err(Error::InvalidGPTEntry);
        }
        Ok(part)
    }

    pub fn find_by_partuuid(
        &mut self,
        uuid: Uuid,
    ) -> Result<Option<PartitionEntry>, Error<IO::Error>> {
        let mut found = None;
        for i in 0..self.count() {
            let part = self.index(i)?;
            if part.get_type_guid().is_nil() {
                continue;
            }
            if part.get_partition_guid() == uuid {
                if found.is_some() {
                    return Err(Error::DuplicatePartitionUuid);
                }
                found = Some(part);
            }
        }
        Ok(found)
    }
}
