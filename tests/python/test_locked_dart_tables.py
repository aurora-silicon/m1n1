"""Check locked three-level DART table pages before lifetime reservation."""
from pathlib import Path
import os
import re
import subprocess


def function(source, name):
    match = re.search(r"^(?:static )?(?:bool|int) " + name + r"\([^;]*?\)\s*\{", source, re.M | re.S)
    assert match, name
    end, depth = match.end(), 1
    while depth:
        depth += (source[end] == "{") - (source[end] == "}")
        end += 1
    return source[match.start():end]


def test_locked_dart_tables(tmp_path):
    repo = Path(__file__).resolve().parents[2]
    source = Path(os.environ.get("DART_TABLE_SOURCE", repo / "src/dart.c")).read_text()
    bodies = "\n".join(function(source, name) for name in ("dart_table_in_dram", "dart_collect_locked_tables"))
    harness = r'''#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <sys/mman.h>
typedef uint64_t u64; typedef uint32_t u32;
#define BIT(n) (1ULL<<(n))
#define GENMASK(h,l) (((~0ULL)>>(63-(h)))&((~0ULL)<<(l)))
#define FIELD_GET(mask,x) (((x)&(mask))>>__builtin_ctzll(mask))
#define FIELD_PREP(mask,x) (((u64)(x)<<__builtin_ctzll(mask))&(mask))
#define ARRAY_SIZE(x) (sizeof(x)/sizeof((x)[0]))
#define SZ_16K 16384
#define DART_T8110 1
#define DART_TCR(dart) 0
#define DART_TTBR(dart,idx) 4
#define DART_T8110_TCR_TRANSLATE_ENABLE BIT(0)
#define DART_T8110_TTBR_VALID BIT(0)
#define DART_T8110_TTBR_ADDR GENMASK(29,2)
#define DART_T8110_TTBR_SHIFT 14
#define DART_PTE_VALID BIT(0)
#define DART_PTE_OFFSET_SHIFT 14
struct params {int ttbr_count;};
typedef struct {int type;bool locked,keep;struct params *params;u64 *l1[1];} dart_dev_t;
static u32 tcr,ttbr;
static int changed_tcr,changed_ttbr,tcr_reads,ttbr_reads;
static u32 read32(unsigned address) {
 if(address==0)return (++tcr_reads==2 && changed_tcr)?tcr^8:tcr;
 assert(address==4);
 return (++ttbr_reads==2 && changed_ttbr)?ttbr^4:ttbr;
}
''' + bodies + r'''
static u64 base;
static u64 *root;
static struct params params={1};
static dart_dev_t dart;
static u64 pages[4];static size_t count;
static u64 entry(u64 pa){return FIELD_PREP(GENMASK(37,10),pa>>14)|1;}
static void setup(void){
 memset(root,0,SZ_16K);memset(pages,0,sizeof(pages));count=99;
 dart=(dart_dev_t){.type=DART_T8110,.locked=true,.keep=true,.params=&params,.l1={root}};
 params.ttbr_count=1;tcr=1;ttbr=FIELD_PREP(DART_T8110_TTBR_ADDR,base>>14)|1;
 changed_tcr=changed_ttbr=tcr_reads=ttbr_reads=0;
}
static int collect(size_t capacity){return dart_collect_locked_tables(&dart,base,4*SZ_16K,pages,capacity,&count);}
static void refuse(void){assert(collect(4)==-1 && count==0);}
/* A portable address hint never overwrites an existing mapping. Refuse a
 * different returned address because the synthetic DART encodes this base. */
static void *map_exact_no_clobber(void *address, size_t length)
{
 void *mapped=mmap(address,length,PROT_READ|PROT_WRITE,MAP_PRIVATE|MAP_ANONYMOUS,-1,0);
 if(mapped!=MAP_FAILED && mapped!=address){
  assert(munmap(mapped,length)==0);
  return MAP_FAILED;
 }
 return mapped;
}
int main(void){
 /* Mach-O may already occupy 4 GiB. Search bounded, representable page
  * addresses without replacing any mapping. Keep the first candidate busy
  * when possible, so Linux also exercises the occupied-address fallback. */
 const u64 first=0x100000000;
 u64 *guard=map_exact_no_clobber((void *)first,4*SZ_16K);
 if(guard!=MAP_FAILED)guard[0]=0x1234abcd;
 root=MAP_FAILED;
 for(unsigned int i=0;i<64;i++){
  base=first+(u64)i*0x10000000;
  root=map_exact_no_clobber((void *)base,4*SZ_16K);
  if(root!=MAP_FAILED)break;
 }
 assert(root!=MAP_FAILED && root==(void *)base);
 if(guard!=MAP_FAILED){
  assert(guard[0]==0x1234abcd);
  assert(base!=first);
  assert(munmap(guard,4*SZ_16K)==0);
 }
 /* An occupied hint must be refused without overwriting the original page. */
 root[0]=0x5a5aa5a5;
 assert(map_exact_no_clobber(root,4*SZ_16K)==MAP_FAILED);
 assert(root[0]==0x5a5aa5a5);
 setup();root[0]=entry(base+SZ_16K);root[2047]=entry(base+2*SZ_16K);root[9]=root[0];
 assert(collect(3)==0 && count==3 && pages[0]==base && pages[1]==base+SZ_16K && pages[2]==base+2*SZ_16K);
 setup();root[0]=entry(base+SZ_16K);assert(collect(1)==-1 && count==0);
 setup();assert(collect(1)==0 && count==1);
 setup();root[0]=entry(base+4*SZ_16K);refuse();
 setup();root[0]=entry(base-SZ_16K);refuse();
 setup();root[0]=entry(base);refuse();
 setup();root[0]=entry(base+SZ_16K)|2;refuse();
 setup();root[0]=entry(base+SZ_16K)&~1ULL;refuse();
 setup();root[0]=entry(base+SZ_16K)|(1ULL<<63);refuse();
 for(int bits=1;bits<16;bits++){setup();tcr=1|(bits<<1);refuse();}
 setup();ttbr&=~1;refuse();
 setup();ttbr^=4;refuse();
 setup();changed_tcr=1;refuse();
 setup();changed_ttbr=1;refuse();
 setup();dart.type=0;refuse();
 setup();dart.locked=false;refuse();
 setup();dart.keep=false;refuse();
 setup();params.ttbr_count=4;refuse();
 setup();dart.l1[0]=(u64 *)(base+1);refuse();
 setup();dart.l1[0]=(u64 *)(base+4*SZ_16K);refuse();
 setup();assert(dart_collect_locked_tables(&dart,base,UINT64_MAX,pages,4,&count)==-1 && !count);
 setup();assert(dart_collect_locked_tables(&dart,base,SZ_16K-1,pages,4,&count)==-1 && !count);
 setup();assert(dart_collect_locked_tables(NULL,base,4*SZ_16K,pages,4,&count)==-1 && !count);
 setup();assert(dart_collect_locked_tables(&dart,base,4*SZ_16K,NULL,4,&count)==-1 && !count);
 setup();assert(dart_collect_locked_tables(&dart,base,4*SZ_16K,pages,0,&count)==-1 && !count);
 setup();assert(dart_collect_locked_tables(&dart,base,4*SZ_16K,pages,4,NULL)==-1);
 assert(munmap(root,4*SZ_16K)==0);
 puts("locked DART: root/leaf DRAM bounds, descriptor encoding, capacity and recheck PASS");
 return 0;
}
'''
    code = tmp_path / "locked-dart-tables.c"
    code.write_text(harness)
    binary = tmp_path / "locked-dart-tables"
    subprocess.run(["cc", "-D_GNU_SOURCE", "-std=c11", "-Wall", "-Wextra", "-Werror", str(code), "-o", str(binary)], check=True)
    subprocess.run([str(binary)], check=True)
