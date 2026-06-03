// Synthetic tiered-memory workload: w% of ACCESSES are writes (op-level), targets
// follow a ZIPFIAN (YCSB-style, theta). Reads = pure loads (clean). The op rate is
// THROTTLED to a realistic ops/s so the write rate doesn't trivially swamp the
// scan -- this is what turns the cliff into a gradient.
//   args: <size_MB> <write_ratio_0_100> <duration_s> [theta] [ops_per_sec]
#define _GNU_SOURCE
#include <stdio.h>
#include <stdlib.h>
#include <stdint.h>
#include <string.h>
#include <time.h>
#include <math.h>
#include <sys/mman.h>

static inline uint64_t xs(uint64_t *s){ uint64_t x=*s; x^=x<<13; x^=x>>7; x^=x<<17; return *s=x; }
static inline double u01(uint64_t *s){ return (xs(s) >> 11) * (1.0/9007199254740992.0); }
static double mono(void){ struct timespec t; clock_gettime(CLOCK_MONOTONIC,&t); return t.tv_sec + t.tv_nsec*1e-9; }

static uint64_t ZN; static double Zth, Zzn, Zalpha, Zeta;
static double zeta(uint64_t n, double t){ double s=0; for(uint64_t i=1;i<=n;i++) s+=pow(1.0/(double)i,t); return s; }
static void zinit(uint64_t n, double t){ ZN=n; Zth=t; double z2=zeta(2,t); Zzn=zeta(n,t); Zalpha=1.0/(1.0-t); Zeta=(1-pow(2.0/n,1-t))/(1-z2/Zzn); }
static uint64_t znext(uint64_t *s){ double u=u01(s), uz=u*Zzn; if(uz<1) return 0; if(uz<1+pow(0.5,Zth)) return 1; uint64_t r=(uint64_t)(ZN*pow(Zeta*u-Zeta+1.0,Zalpha)); return r<ZN?r:ZN-1; }

int main(int argc, char **argv){
    long mb=argc>1?atol(argv[1]):128; int w=argc>2?atoi(argv[2]):10;
    int dur=argc>3?atoi(argv[3]):300; double theta=argc>4?atof(argv[4]):0.99;
    double rate=argc>5?atof(argv[5]):10000.0;          /* ops/s; 0 = unlimited */
    size_t N=(size_t)mb<<20, pages=N>>12;
    char *a=mmap(NULL,N,PROT_READ|PROT_WRITE,MAP_PRIVATE|MAP_ANONYMOUS,-1,0);
    if(a==MAP_FAILED){ perror("mmap"); return 1; }
    memset(a,1,N); zinit(pages,theta);
    printf("INIT DONE mb=%ld w=%d%% dur=%ds pages=%zu theta=%.2f rate=%.0f/s\n",mb,w,dur,pages,theta,rate);
    fflush(stdout);
    uint64_t s=0x9e3779b97f4a7c15ULL; volatile long sum=0; time_t t0=time(NULL);
    long batch=500;
    while(time(NULL)-t0<dur){
        double bs=mono();
        for(long k=0;k<batch;k++){
            uint64_t idx=znext(&s); size_t off=idx<<12;
            if((xs(&s)%100)<(uint64_t)w) a[off]=(char)idx;   /* WRITE */
            else sum+=a[off];                                /* READ (load) */
        }
        if(rate>0){ double tg=batch/rate, el=mono()-bs; if(tg>el){ double d=tg-el; struct timespec ts={(time_t)d,(long)((d-(time_t)d)*1e9)}; nanosleep(&ts,NULL); } }
    }
    printf("done sum=%ld\n",(long)sum); return 0;
}
