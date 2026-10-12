RUSTARCH ?= aarch64-unknown-none-softfloat

ifeq ($(shell uname),Darwin)
USE_CLANG ?= 1
$(info INFO: Building on Darwin)

ifeq ($(shell command -v llvm-config 2>/dev/null),)
BREW ?= $(shell command -v brew)
LLVMCONFIG ?= $(shell $(BREW) --prefix llvm)/bin/llvm-config
else
LLVMCONFIG ?= $(shell command -v llvm-config)
endif
TOOLCHAIN ?= $(shell $(LLVMCONFIG) --bindir)/
$(info INFO: Toolchain path: $(TOOLCHAIN))

ifeq ($(shell ls $(TOOLCHAIN)ld.lld 2>/dev/null),)
BREW ?= $(shell command -v brew)
LLDDIR ?= $(shell $(BREW) --prefix lld)/bin/
else
LLDDIR ?= $(TOOLCHAIN)
endif
ifneq ($(TOOLCHAIN),$(LLDDIR))
$(info INFO: LLD path: $(LLDDIR))
endif
endif

ifeq ($(shell uname -m),aarch64)
ARCH ?=
else
ARCH ?= aarch64-linux-gnu-
endif

ifeq ($(USE_CLANG),1)
CC := $(TOOLCHAIN)clang --target=$(ARCH)
AS := $(TOOLCHAIN)clang --target=$(ARCH)
LD := $(LLDDIR)ld.lld
OBJCOPY := $(TOOLCHAIN)llvm-objcopy
CLANG_FORMAT ?= $(TOOLCHAIN)clang-format
EXTRA_CFLAGS ?=
else
CC := $(TOOLCHAIN)$(ARCH)gcc
AS := $(TOOLCHAIN)$(ARCH)gcc
LD := $(TOOLCHAIN)$(ARCH)ld
OBJCOPY := $(TOOLCHAIN)$(ARCH)objcopy
CLANG_FORMAT ?= clang-format
EXTRA_CFLAGS ?= -Wstack-usage=2048
endif

ifeq ($(V),)
QUIET := @
else
ifeq ($(V),0)
QUIET := @
else
QUIET :=
endif
endif

BASE_CFLAGS := -O2 -Wall -g -Wundef -Werror=strict-prototypes -fno-common -fno-PIE \
	-Werror=implicit-function-declaration -Werror=implicit-int \
	-Wsign-compare -Wunused-parameter -Wno-multichar \
	-ffreestanding -fpic -ffunction-sections -fdata-sections \
	-nostdinc -isystem $(shell $(CC) -print-file-name=include) -isystem sysinc \
	-Isrc \
	-fno-stack-protector -mstrict-align -march=armv8.2-a \
	$(EXTRA_CFLAGS)

CFLAGS := $(BASE_CFLAGS) -mgeneral-regs-only

CFG :=
ifeq ($(RELEASE),1)
CFG += RELEASE
endif

# Required for no_std + alloc for now
export RUSTC_BOOTSTRAP=1
RUST_LIB := librust.a
ifeq ($(BUILDSTD),1)
CARGO_FLAGS := -Z build-std=alloc,core
else
CARGO_FLAGS :=
endif

ifeq ($(CHAINLOADING),1)
CFG += CHAINLOADING
CARGO_FLAGS += --features chainload
endif

# Stage 1 for macOS 26.6.2 OS firmware that loads one stage 2 file from the
# internal EFI system partition (see src/stage1.c). It is built for one board:
# - J613_STAGE1=1: MacBook Air 13" M3 (J613)
# - J615_STAGE1=1: MacBook Air 15-inch M3 (J615). Experimental: not yet tested
#   on hardware.
STAGE1_BOARD :=
ifeq ($(J613_STAGE1),1)
STAGE1_BOARD := J613
endif
ifeq ($(J615_STAGE1),1)
ifneq ($(STAGE1_BOARD),)
$(error J613_STAGE1=1 and J615_STAGE1=1 cannot be combined)
endif
STAGE1_BOARD := J615
endif

ifneq ($(STAGE1_BOARD),)
ifneq ($(CHAINLOADING),1)
$(error $(STAGE1_BOARD)_STAGE1=1 needs CHAINLOADING=1)
endif
CFG += ESP_STAGE1 ESP_STAGE1_$(STAGE1_BOARD)
ifeq ($(strip $(M1N1_VERSION_TAG)),)
M1N1_VERSION_TAG := v1.6.1-m3air.stage1
endif
export M1N1_VERSION_TAG
ifneq ($(shell printf '%s\n' '$(M1N1_VERSION_TAG)' | LC_ALL=C grep -Ex '[A-Za-z0-9._+-]+'),$(M1N1_VERSION_TAG))
$(error M1N1_VERSION_TAG must match [A-Za-z0-9._+-]+)
endif
ifneq ($(LOGO),)
$(error LOGO is not supported with $(STAGE1_BOARD)_STAGE1=1)
endif

# Size rule for the installed raw image (S). On 26.6.2, iBoot places live
# firmware log buffers (0x5f000 bytes) directly after the installed object, and
# the chainloaded stage 2 is copied to the same base, so its bytes in
# [S, S + 0x5f000) are overwritten by firmware:
# - S >= 0x114000 keeps that range clear of a 0x114000-byte stage 2 m1n1;
# - S + 0x5f000 <= 0x3e0000 keeps it inside the zero padding that the Aurora
#   m1n1 stage 2 no longer uses once it has started.
# This layout was measured on J613. J615 has the same SoC and OS firmware and
# uses the same bounds, which have not yet been checked on a J615.
STAGE1_MIN_SIZE := 0x114000
STAGE1_MAX_SIZE := 0x381000
STAGE1_CHECK = $(QUIET)size=$$(wc -c < $@); \
	if [ $$size -lt $$(($(STAGE1_MIN_SIZE))) ] || [ $$size -gt $$(($(STAGE1_MAX_SIZE))) ]; then \
		printf 'error: %s is 0x%x bytes; a $(STAGE1_BOARD) stage 1 must be %s..%s bytes\n' \
			$@ $$size $(STAGE1_MIN_SIZE) $(STAGE1_MAX_SIZE) >&2; \
		rm -f $@; exit 1; \
	fi; \
	if [ "$$(tail -c 8 $@)" != STACKBOT ]; then \
		echo "error: $@ does not end with the STACKBOT marker" >&2; rm -f $@; exit 1; \
	fi; \
	printf '  SIZE  %s 0x%x bytes (allowed %s..%s)\n' $@ $$size $(STAGE1_MIN_SIZE) $(STAGE1_MAX_SIZE)
endif

LDFLAGS := -EL -maarch64elf --no-undefined -X -Bsymbolic \
	-z notext --no-apply-dynamic-relocs --orphan-handling=warn \
	-z nocopyreloc --gc-sections -pie

MINILZLIB_OBJECTS := $(patsubst %,minilzlib/%, \
	dictbuf.o inputbuf.o lzma2dec.o lzmadec.o rangedec.o xzstream.o)

TINF_OBJECTS := $(patsubst %,tinf/%, \
	adler32.o crc32.o tinfgzip.o tinflate.o tinfzlib.o)

DLMALLOC_OBJECTS := dlmalloc/malloc.o

LIBFDT_OBJECTS := $(patsubst %,libfdt/%, \
	fdt_addresses.o fdt_empty_tree.o fdt_ro.o fdt_rw.o fdt_strerror.o fdt_sw.o \
	fdt_wip.o fdt.o)

CHICKENS_OBJECTS := $(patsubst %,chickens/%, \
	avalanche.o \
	blizzard.o \
	cyclone_typhoon.o \
	everest.o \
	firestorm.o \
	hurricane_zephyr.o \
	icestorm.o \
	monsoon_mistral.o \
	sawtooth.o \
	twister.o)

DCP_OBJECTS := $(patsubst %,dcp/%, \
	dpav_ep.o \
	dptx_phy.o \
	dptx_port_ep.o \
	parser.o \
	system_ep.o)

OBJECTS := \
	adt.o \
	afk.o \
	aic.o \
	asc.o \
	bootlogo_48.o bootlogo_128.o bootlogo_256.o \
	chainload.o \
	chainload_asm.o \
	chickens.o \
	clk.o \
	cpufreq.o \
	dapf.o \
	dart.o \
	dcp.o \
	dcp_iboot.o \
	devicetree.o \
	display.o \
	dockchannel_uart.o \
	exception.o exception_asm.o \
	fb.o font.o font_retina.o \
	firmware.o \
	gxf.o gxf_asm.o \
	heapblock.o \
	hv.o hv_vm.o hv_exc.o hv_vuart.o hv_wdt.o hv_asm.o hv_aic.o hv_virtio.o \
	i2c.o \
	iodev.o \
	iova.o \
	isp.o \
	kboot.o kboot_atc.o \
	kboot_t6020_compat.o \
	main.o \
	mitigations.o \
	mcc.o \
	memory.o memory_asm.o \
	nvme.o \
	payload.o \
	pcie.o \
	pmgr.o \
	proxy.o \
	ringbuffer.o \
	rtkit.o \
	sart.o \
	sep.o \
	sio.o \
	smc.o \
	smp.o \
	spmi.o \
	stage1.o stage1_cfg.o \
	start.o \
	startup.o \
	string.o \
	tunables.o tunables_static.o \
	tps6598x.o \
	uart.o \
	uartproxy.o \
	usb.o usb_dwc3.o \
	utils.o utils_asm.o \
	vsprintf.o \
	wdt.o \
	$(CHICKENS_OBJECTS) \
	$(DCP_OBJECTS) \
	$(MINILZLIB_OBJECTS) $(TINF_OBJECTS) $(DLMALLOC_OBJECTS) $(LIBFDT_OBJECTS)

FP_OBJECTS := \
	kboot_gpu.o \
	math/expf.o \
	math/exp2f_data.o \
	math/powf.o \
	math/powf_data.o

BUILD_OBJS := $(patsubst %,build/%,$(OBJECTS))
BUILD_FP_OBJS := $(patsubst %,build/%,$(FP_OBJECTS))
BUILD_RUST_LIB := $(patsubst %,build/%,$(RUST_LIB))
BUILD_ALL_OBJS := $(BUILD_OBJS) $(BUILD_FP_OBJS) $(BUILD_RUST_LIB)
NAME := m1n1
TARGET := m1n1.macho
TARGET_RAW := m1n1.bin

DEPDIR := build/.deps

.PHONY: all clean format invoke_cc always_rebuild
all: build/$(TARGET) build/$(TARGET_RAW)
clean:
	rm -rf build/* build/.deps
format:
	$(CLANG_FORMAT) -i src/*.c src/chickens/*.c src/dcp/*.c src/math/*.c src/*.h src/dcp/*.h src/math/*.h sysinc/*.h
format-check:
	$(CLANG_FORMAT) --dry-run --Werror src/*.c src/chickens/*.c src/dcp/*.c src/math/*.c src/*.h src/dcp/*.h src/math/*.h sysinc/*.h
rustfmt:
	cd rust && cargo fmt
rustfmt-check:
	cd rust && cargo fmt --check

build/$(RUST_LIB): src/../build/build_cfg.h rust/src/*.rs rust/src/gpu/*.rs rust/src/gpu/hw/*.rs rust/Cargo.toml rust/Cargo.lock
	$(QUIET)echo "  RS    $@"
	$(QUIET)mkdir -p $(DEPDIR)
	$(QUIET)mkdir -p "$(dir $@)"
	$(QUIET)cargo build $(CARGO_FLAGS) --target $(RUSTARCH) --lib --release --manifest-path rust/Cargo.toml --target-dir build
	$(QUIET)cp "build/$(RUSTARCH)/release/${RUST_LIB}" "$@"

build/%.o: src/%.S
	$(QUIET)echo "  AS    $@"
	$(QUIET)mkdir -p $(DEPDIR)
	$(QUIET)mkdir -p "$(dir $@)"
	$(QUIET)$(AS) -c $(BASE_CFLAGS) -MMD -MF $(DEPDIR)/$(*F).d -MQ "$@" -MP -o $@ $<

$(BUILD_FP_OBJS): build/%.o: src/%.c
	$(QUIET)echo "  CC FP $@"
	$(QUIET)mkdir -p $(DEPDIR)
	$(QUIET)mkdir -p "$(dir $@)"
	$(QUIET)$(CC) -c $(BASE_CFLAGS) -MMD -MF $(DEPDIR)/$(*F).d -MQ "$@" -MP -o $@ $<

build/%.o: src/%.c build-tag build-cfg
	$(QUIET)echo "  CC    $@"
	$(QUIET)mkdir -p $(DEPDIR)
	$(QUIET)mkdir -p "$(dir $@)"
	$(QUIET)$(CC) -c $(CFLAGS) -MMD -MF $(DEPDIR)/$(*F).d -MQ "$@" -MP -o $@ $<

# special target for usage by m1n1.loadobjs
invoke_cc:
	$(QUIET)$(CC) -c $(CFLAGS) -Isrc -o $(OBJFILE) $(CFILE)

build/$(NAME).elf: $(BUILD_ALL_OBJS) m1n1.ld
	$(QUIET)echo "  LD    $@"
	$(QUIET)$(LD) -T m1n1.ld $(LDFLAGS) -o $@ $(BUILD_ALL_OBJS)

build/$(NAME)-raw.elf: $(BUILD_ALL_OBJS) m1n1-raw.ld
	$(QUIET)echo "  LDRAW $@"
	$(QUIET)$(LD) -T m1n1-raw.ld $(LDFLAGS) -o $@ $(BUILD_ALL_OBJS)

build/$(NAME).macho: build/$(NAME).elf
	$(QUIET)echo "  MACHO $@"
	$(QUIET)$(OBJCOPY) -O binary --strip-debug $< $@

ifeq ($(LOGO),)
build/$(NAME).bin: build/$(NAME)-raw.elf
	$(QUIET)echo "  RAW   $@"
	$(QUIET)$(OBJCOPY) -O binary --strip-debug $< $@
	$(STAGE1_CHECK)

else
build/$(NAME)-asahi.bin: build/$(NAME)-raw.elf
	$(QUIET)echo "  RAW   $@"
	$(QUIET)$(OBJCOPY) -O binary --strip-debug $< $@

build/$(NAME).bin: build/$(NAME)-asahi.bin build/$(LOGO).logo
	$(QUIET)echo "  RAW   $@"
	$(QUIET)cat $^ > $@
endif

.INTERMEDIATE: build-tag build-cfg
build-tag src/../build/build_tag.h &:
	$(QUIET)mkdir -p build
	$(QUIET)./version.sh > build/build_tag.tmp
	$(QUIET)cmp -s build/build_tag.h build/build_tag.tmp 2>/dev/null || \
	( mv -f build/build_tag.tmp build/build_tag.h && echo "  TAG   build/build_tag.h" )

build-cfg src/../build/build_cfg.h &:
	$(QUIET)mkdir -p build
	$(QUIET)for i in $(CFG); do echo "#define $$i"; done > build/build_cfg.tmp
	$(QUIET)cmp -s build/build_cfg.h build/build_cfg.tmp 2>/dev/null || \
	( mv -f build/build_cfg.tmp build/build_cfg.h && echo "  CFG   build/build_cfg.h" )

build/%.bin: data/%.bin
	$(QUIET)echo "  IMG   $@"
	$(QUIET)mkdir -p "$(dir $@)"
	$(QUIET)cp $< $@

build/%.o: build/%.bin
	$(QUIET)echo "  BIN   $@"
	$(QUIET)mkdir -p "$(dir $@)"
	$(QUIET)$(OBJCOPY) -I binary -B aarch64 -O elf64-littleaarch64 $< $@

build/%.bin: font/%.bin
	$(QUIET)echo "  CP    $@"
	$(QUIET)mkdir -p "$(dir $@)"
	$(QUIET)cp $< $@

build/%.rgba: data/%.png
	$(eval SIZE := $(lastword $(subst _, ,$*)))
	$(QUIET)echo "  MAGIC $@"
	$(QUIET)mkdir -p "$(dir $@)"
	$(QUIET)magick $< -background black -flatten -depth 8 -crop $(SIZE)x$(SIZE) -resize $(SIZE)x$(SIZE) rgba:$@

build/%.logo: build/%_256.rgba build/%_128.rgba
	$(QUIET)echo "  PAYLOAD $@"
	$(QUIET)mkdir -p "$(dir $@)"
	$(QUIET)echo -n "m1n1_logo_256128" > $@
	$(QUIET)cat $^ >> $@

-include $(DEPDIR)/*
