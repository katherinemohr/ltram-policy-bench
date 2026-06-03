#!/usr/bin/env python3
import sys, pandas as pd, matplotlib
matplotlib.use("Agg"); import matplotlib.pyplot as plt
csv=sys.argv[1]; out=sys.argv[2] if len(sys.argv)>2 else csv.rsplit("/",1)[0]+"/synthsweep.png"
df=pd.read_csv(csv, comment="#").sort_values("write_ratio")
U,W="tab:blue","#d95f02"
fig,ax1=plt.subplots(figsize=(8.5,5))
l1,=ax1.plot(df.write_ratio,df.peak_util_pct,"o-",color=U,linewidth=1.8,label="LtRAM utilization")
ax1.set_xlabel("Write ratio (% of accesses that are writes)",fontsize=11)
ax1.set_ylabel("LtRAM Utilization (%)\nPages in LtRAM / Total Memory Usage",color=U,fontsize=11)
ax1.tick_params(axis="y",labelcolor=U); ax1.set_ylim(0,100); ax1.set_xlim(0,100)
ax2=ax1.twinx()
l2,=ax2.plot(df.write_ratio,df.final_writeback_pct,"D--",color=W,linewidth=1.6,markersize=5,label="Write-back")
ax2.set_ylabel("Write-back (%)\nPages moved out of LtRAM / moved in",color=W,fontsize=11)
ax2.tick_params(axis="y",labelcolor=W); ax2.set_ylim(0,max(5,df.final_writeback_pct.max()*1.2))
ax1.legend(handles=[l1,l2],loc="upper center",framealpha=0.9)
ax1.set_title("Synthetic zipfian workload: LtRAM offload vs write ratio",fontsize=12)
ax1.grid(True,alpha=0.3); fig.tight_layout(); fig.savefig(out,dpi=130); print("wrote",out)
