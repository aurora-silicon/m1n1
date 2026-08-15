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

# Must be defined BEFORE BASE_CFLAGS: that is a `:=` (immediately expanded)
# assignment, so an empty BUILD_DIR here turns its -I$(BUILD_DIR) into a bare
# -I that swallows the following -fno-stack-protector as its argument. The
# build/ directory then is not on the include path at all and every object
# that includes the generated build_cfg.h fails with "file not found".
# `make BUILD_DIR=... ` (how tools/build-j414s-windows-unified.py invokes it)
# masked this, because a command-line assignment is in scope before the
# makefile is read; a plain `make` from a clean tree did not.
BUILD_DIR ?= build

BASE_CFLAGS := -O2 -Wall -g -Wundef -Werror=strict-prototypes -fno-common -fno-PIE \
	-Werror=implicit-function-declaration -Werror=implicit-int \
	-Wsign-compare -Wunused-parameter -Wno-multichar \
	-ffreestanding -fpic -ffunction-sections -fdata-sections \
	-nostdinc -isystem $(shell $(CC) -print-file-name=include) -isystem sysinc -Isrc -I$(BUILD_DIR) \
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

ifeq ($(RESIDENT_STAGE1),1)
CFG += RESIDENT_STAGE1
endif

RUST_FLAGS_STAMP := $(BUILD_DIR)/.rust-cargo-flags

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
	ace3.o \
	adt.o \
	afk.o \
	aic.o \
	asc.o \
	acio.o acio_type5.o acio_runtime.o \
	atcphy.o atcphy_core.o \
	bcm4388_handoff.o \
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
	gpio.o \
	gpu_handoff.o gpu_handoff_abi.o \
	gxf.o gxf_asm.o \
	heapblock.o \
	hv.o hv_vm.o hv_exc.o hv_vuart.o hv_wdt.o hv_asm.o hv_aic.o hv_aic_alias.o hv_virtio.o hv_tpm.o hv_xfer.o fb_capture.o hv_psci.o hv_vgic.o \
	i2c.o \
	iodev.o \
	iova.o \
	isp.o \
	kboot.o kboot_atc.o \
	kboot_t6020_compat.o \
	main.o \
	media_handoff.o \
	mitigations.o \
	mcc.o \
	memory.o memory_asm.o \
	mtp_handoff.o \
	nvme.o \
	payload.o \
	pcie.o \
	wireless_handoff.o wireless_handoff_abi.o \
	pmgr.o \
	platform_identity.o \
	proxy.o \
	ringbuffer.o \
	rtkit.o \
	sart.o \
	sep.o \
	sio.o \
	smc.o \
	smp.o \
	spmi.o \
	start.o \
	startup.o \
	string.o \
	tunables.o tunables_static.o \
	tps6598x.o tps6598x_host_policy.o \
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

BUILD_OBJS := $(patsubst %,$(BUILD_DIR)/%,$(OBJECTS))
BUILD_FP_OBJS := $(patsubst %,$(BUILD_DIR)/%,$(FP_OBJECTS))
BUILD_RUST_LIB := $(patsubst %,$(BUILD_DIR)/%,$(RUST_LIB))
BUILD_ALL_OBJS := $(BUILD_OBJS) $(BUILD_FP_OBJS) $(BUILD_RUST_LIB)
NAME := m1n1
TARGET := m1n1.macho
TARGET_RAW := m1n1.bin

DEPDIR := $(BUILD_DIR)/.deps

.PHONY: all clean format invoke_cc always_rebuild
all: $(BUILD_DIR)/$(TARGET) $(BUILD_DIR)/$(TARGET_RAW)
clean:
	rm -rf $(BUILD_DIR)/* $(BUILD_DIR)/.deps
format:
	$(CLANG_FORMAT) -i src/*.c src/chickens/*.c src/dcp/*.c src/math/*.c src/*.h src/dcp/*.h src/math/*.h sysinc/*.h
format-check:
	$(CLANG_FORMAT) --dry-run --Werror src/*.c src/chickens/*.c src/dcp/*.c src/math/*.c src/*.h src/dcp/*.h src/math/*.h sysinc/*.h
rustfmt:
	cd rust && cargo fmt
rustfmt-check:
	cd rust && cargo fmt --check

$(RUST_FLAGS_STAMP): FORCE
	$(QUIET)mkdir -p $(BUILD_DIR)
	$(QUIET)printf '%s\n' "$(strip $(CARGO_FLAGS))" > $(RUST_FLAGS_STAMP).tmp
	$(QUIET)cmp -s $(RUST_FLAGS_STAMP) $(RUST_FLAGS_STAMP).tmp 2>/dev/null || \
	( mv -f $(RUST_FLAGS_STAMP).tmp $(RUST_FLAGS_STAMP) && echo "  CFG   $(RUST_FLAGS_STAMP)" )
	$(QUIET)rm -f $(RUST_FLAGS_STAMP).tmp

$(BUILD_DIR)/$(RUST_LIB): $(RUST_FLAGS_STAMP) rust/src/*.rs rust/src/gpu/*.rs rust/src/gpu/hw/*.rs rust/Cargo.toml rust/Cargo.lock
	$(QUIET)echo "  RS    $@"
	$(QUIET)mkdir -p $(DEPDIR)
	$(QUIET)mkdir -p "$(dir $@)"
	$(QUIET)cargo build $(CARGO_FLAGS) --target $(RUSTARCH) --lib --release --manifest-path rust/Cargo.toml --target-dir $(BUILD_DIR)
	$(QUIET)cp "$(BUILD_DIR)/$(RUSTARCH)/release/${RUST_LIB}" "$@"

$(BUILD_DIR)/%.o: src/%.S
	$(QUIET)echo "  AS    $@"
	$(QUIET)mkdir -p $(DEPDIR)
	$(QUIET)mkdir -p "$(dir $@)"
	$(QUIET)$(AS) -c $(BASE_CFLAGS) -MMD -MF $(DEPDIR)/$(*F).d -MQ "$@" -MP -o $@ $<

$(BUILD_FP_OBJS): $(BUILD_DIR)/%.o: src/%.c
	$(QUIET)echo "  CC FP $@"
	$(QUIET)mkdir -p $(DEPDIR)
	$(QUIET)mkdir -p "$(dir $@)"
	$(QUIET)$(CC) -c $(BASE_CFLAGS) -MMD -MF $(DEPDIR)/$(*F).d -MQ "$@" -MP -o $@ $<

$(BUILD_DIR)/%.o: src/%.c build-tag build-cfg
	$(QUIET)echo "  CC    $@"
	$(QUIET)mkdir -p $(DEPDIR)
	$(QUIET)mkdir -p "$(dir $@)"
	$(QUIET)$(CC) -c $(CFLAGS) -MMD -MF $(DEPDIR)/$(*F).d -MQ "$@" -MP -o $@ $<

# special target for usage by m1n1.loadobjs
invoke_cc:
	$(QUIET)$(CC) -c $(CFLAGS) -Isrc -o $(OBJFILE) $(CFILE)

$(BUILD_DIR)/$(NAME).elf: $(BUILD_ALL_OBJS) m1n1.ld
	$(QUIET)echo "  LD    $@"
	$(QUIET)$(LD) -T m1n1.ld $(LDFLAGS) -o $@ $(BUILD_ALL_OBJS)

$(BUILD_DIR)/$(NAME)-raw.elf: $(BUILD_ALL_OBJS) m1n1-raw.ld
	$(QUIET)echo "  LDRAW $@"
	$(QUIET)$(LD) -T m1n1-raw.ld $(LDFLAGS) -o $@ $(BUILD_ALL_OBJS)

$(BUILD_DIR)/$(NAME).macho: $(BUILD_DIR)/$(NAME).elf
	$(QUIET)echo "  MACHO $@"
	$(QUIET)$(OBJCOPY) -O binary --strip-debug $< $@

ifeq ($(LOGO),)
$(BUILD_DIR)/$(NAME).bin: $(BUILD_DIR)/$(NAME)-raw.elf
	$(QUIET)echo "  RAW   $@"
	$(QUIET)$(OBJCOPY) -O binary --strip-debug $< $@

else
$(BUILD_DIR)/$(NAME)-asahi.bin: $(BUILD_DIR)/$(NAME)-raw.elf
	$(QUIET)echo "  RAW   $@"
	$(QUIET)$(OBJCOPY) -O binary --strip-debug $< $@

$(BUILD_DIR)/$(NAME).bin: $(BUILD_DIR)/$(NAME)-asahi.bin $(BUILD_DIR)/$(LOGO).logo
	$(QUIET)echo "  RAW   $@"
	$(QUIET)cat $^ > $@
endif

.PHONY: build-tag build-cfg FORCE
build-tag: $(BUILD_DIR)/build_tag.h
$(BUILD_DIR)/build_tag.h: FORCE
	$(QUIET)mkdir -p $(BUILD_DIR)
	$(QUIET)./version.sh > $(BUILD_DIR)/build_tag.tmp
	$(QUIET)cmp -s $(BUILD_DIR)/build_tag.h $(BUILD_DIR)/build_tag.tmp 2>/dev/null || \
	( mv -f $(BUILD_DIR)/build_tag.tmp $(BUILD_DIR)/build_tag.h && echo "  TAG   $(BUILD_DIR)/build_tag.h" )

build-cfg: $(BUILD_DIR)/build_cfg.h
$(BUILD_DIR)/build_cfg.h: FORCE
	$(QUIET)mkdir -p $(BUILD_DIR)
	$(QUIET)for i in $(CFG); do echo "#define $$i"; done > $(BUILD_DIR)/build_cfg.tmp
	$(QUIET)cmp -s $(BUILD_DIR)/build_cfg.h $(BUILD_DIR)/build_cfg.tmp 2>/dev/null || \
	( mv -f $(BUILD_DIR)/build_cfg.tmp $(BUILD_DIR)/build_cfg.h && echo "  CFG   $(BUILD_DIR)/build_cfg.h" )

FORCE:

$(BUILD_DIR)/%.bin: data/%.bin
	$(QUIET)echo "  IMG   $@"
	$(QUIET)mkdir -p "$(dir $@)"
	$(QUIET)cp $< $@

$(BUILD_DIR)/%.o: $(BUILD_DIR)/%.bin
	$(QUIET)echo "  BIN   $@"
	$(QUIET)mkdir -p "$(dir $@)"
	$(QUIET)cd "$(dir $@)" && $(OBJCOPY) -I binary -B aarch64 -O elf64-littleaarch64 \
		"$(notdir $<)" "$(notdir $@)"

$(BUILD_DIR)/%.bin: font/%.bin
	$(QUIET)echo "  CP    $@"
	$(QUIET)mkdir -p "$(dir $@)"
	$(QUIET)cp $< $@

$(BUILD_DIR)/%.rgba: data/%.png
	$(eval SIZE := $(lastword $(subst _, ,$*)))
	$(QUIET)echo "  MAGIC $@"
	$(QUIET)mkdir -p "$(dir $@)"
	$(QUIET)magick $< -background black -flatten -depth 8 -crop $(SIZE)x$(SIZE) -resize $(SIZE)x$(SIZE) rgba:$@

$(BUILD_DIR)/%.logo: $(BUILD_DIR)/%_256.rgba $(BUILD_DIR)/%_128.rgba
	$(QUIET)echo "  PAYLOAD $@"
	$(QUIET)mkdir -p "$(dir $@)"
	$(QUIET)echo -n "m1n1_logo_256128" > $@
	$(QUIET)cat $^ >> $@

-include $(DEPDIR)/*
