// SPDX-License-Identifier: MIT
// Generated-by: LLM
// Assisted-by: LLM
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <libfdt.h>
typedef uint32_t u32;
/* libfdt requires an eight-byte-aligned blob on every host compiler. */
_Alignas(8) static unsigned char blob[32768], before[32768];
static void *dt = blob;
static size_t dt_bufsize = sizeof(blob);
static int fail_write, writes;
static int test_setprop(void *fdt, int n, const char *p, const void *v, int l)
{
    if (fail_write && ++writes == fail_write)
        return -FDT_ERR_NOSPACE;
    return fdt_setprop(fdt,n,p,v,l);
}
#define fdt_setprop test_setprop
#include "helper.c"
#undef fdt_setprop
static int node(const char *path) { int n=fdt_path_offset(dt,path);assert(n>=0);return n; }
static void cells(const char *path,const char *key,const u32 *v,int n)
{
    fdt32_t b[100];for(int i=0;i<n;i++)b[i]=cpu_to_fdt32(v[i]);
    assert(!fdt_setprop(dt,node(path),key,b,n*4));
}
static void add(const char *name) { assert(fdt_add_subnode(dt,0,name)>=0); }
static void provider(const char *name,u32 ph,u32 nargs)
{
    char path[64];add(name);snprintf(path,sizeof(path),"/%s",name);
    cells(path,"phandle",&ph,1);cells(path,"#iommu-cells",&nargs,1);
}
static void master(const char *name,const u32 *v,int n)
{
    char path[64];add(name);snprintf(path,sizeof(path),"/%s",name);
    assert(!fdt_setprop_string(dt,node(path),"compatible","apple,display-subsystem"));
    cells(path,"iommus",v,n);
}
static void setup(void)
{
    memset(blob,0,sizeof(blob));assert(!fdt_create_empty_tree(dt,sizeof(blob)));
    fail_write=writes=0;provider("internal",11,2);provider("external-dart",22,1);
    provider("zeroarg",33,0);add("dcpext");
    assert(fdt_add_subnode(dt,node("/dcpext"),"piodma")>=0);
    u32 ext[]={22,16};cells("/dcpext/piodma","iommus",ext,2);
}
static void same(void) { assert(!memcmp(blob,before,sizeof(blob))); }
static void expect(const char *path,const u32 *v,int count)
{
    int len;const fdt32_t *a=fdt_getprop(dt,node(path),"iommus",&len);
    assert(a && len==count*4);for(int i=0;i<count;i++)assert(fdt32_to_cpu(a[i])==v[i]);
}
static int run(void) { return dt_t8140_withdraw_external_scanout(node("/dcpext")); }
int main(void)
{
    setup();u32 mix[]={11,22,99,22,0,22,16,33,11,3,4};u32 keep[]={11,22,99,33,11,3,4};
    master("display-a",mix,11);master("display-b",mix,11);assert(!run());
    expect("/display-a",keep,7);expect("/display-b",keep,7);puts("PASS mixed-width, argument collision, duplicates and two shifting masters");
    setup();u32 only[]={22,0,22,16};master("display",only,4);assert(!run());expect("/display",only,0);puts("PASS all external specifiers removed");
    setup();u32 clean[]={11,1,2,33};master("display",clean,4);memcpy(before,blob,sizeof(blob));assert(!run());same();puts("PASS no external references unchanged");
    setup();assert(!fdt_del_node(dt,node("/dcpext/piodma")));memcpy(before,blob,sizeof(blob));assert(!run());same();puts("PASS missing optional piodma unchanged");
    setup();assert(!fdt_delprop(dt,node("/dcpext/piodma"),"iommus"));memcpy(before,blob,sizeof(blob));assert(!run());same();puts("PASS absent optional iommus unchanged");
    setup();u32 bad[]={11,1,2,22};master("display",bad,4);memcpy(before,blob,sizeof(blob));assert(run()<0);same();puts("PASS truncated master rejected before edits");
    setup();u32 unknown[]={99,0};master("display",unknown,2);memcpy(before,blob,sizeof(blob));assert(run()<0);same();puts("PASS unknown provider rejected unchanged");
    setup();u32 badext[]={22};cells("/dcpext/piodma","iommus",badext,1);memcpy(before,blob,sizeof(blob));assert(run()<0);same();puts("PASS truncated piodma rejected unchanged");
    setup();u32 excessive=17;cells("/external-dart","#iommu-cells",&excessive,1);memcpy(before,blob,sizeof(blob));assert(run()<0);same();puts("PASS excessive provider width rejected unchanged");
    setup();u32 longrefs[66];for(int i=0;i<66;i++)longrefs[i]=33;master("display",longrefs,66);memcpy(before,blob,sizeof(blob));assert(run()<0);same();puts("PASS oversized property rejected unchanged");
    setup();for(int i=0;i<9;i++){char name[32];snprintf(name,sizeof(name),"display%d",i);master(name,only,4);}memcpy(before,blob,sizeof(blob));assert(run()<0);same();puts("PASS ninth changed master rejected atomically");
    setup();master("display-a",mix,11);master("display-b",mix,11);memcpy(before,blob,sizeof(blob));fail_write=2;assert(run()<0);same();puts("PASS second write failure restores whole real FDT");
    setup();master("display",only,4);unsigned char odd[3]={0};assert(!fdt_setprop(dt,node("/display"),"iommus",odd,3));memcpy(before,blob,sizeof(blob));assert(run()<0);same();puts("PASS non-cell-aligned master property rejected unchanged");
    setup();assert(!fdt_setprop(dt,node("/dcpext/piodma"),"iommus",odd,3));memcpy(before,blob,sizeof(blob));assert(run()<0);same();puts("PASS non-cell-aligned piodma property rejected unchanged");
    setup();assert(!fdt_delprop(dt,node("/external-dart"),"#iommu-cells"));memcpy(before,blob,sizeof(blob));assert(run()<0);same();puts("PASS missing provider width rejected unchanged");
    return 0;
}
