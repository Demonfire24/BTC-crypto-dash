// Test harness: mirrors HigherHighLowerLowStrategy.mq5 line for line so that the
// algorithm (and above all the incremental OnCalculate() bookkeeping) can be
// exercised outside MetaTrader.  A second, deliberately naive implementation of
// the Pine source is used as the oracle.
#include <cstdio>
#include <cmath>
#include <string>
#include <vector>
#include <map>
#include <set>
#include <random>
#include <algorithm>
#include <cassert>

static const double PINE_NA = 1.7976931348623157e+308; // EMPTY_VALUE / DBL_MAX

static bool IsNa(double v){ return v==PINE_NA || !std::isfinite(v); }
static bool Truthy(double v){ return !IsNa(v) && v!=0.0; }
static bool EQ(double a,double b){ return !IsNa(a)&&!IsNa(b)&&a==b; }
static bool GT(double a,double b){ return !IsNa(a)&&!IsNa(b)&&a>b; }
static bool LT(double a,double b){ return !IsNa(a)&&!IsNa(b)&&a<b; }
static bool GE(double a,double b){ return !IsNa(a)&&!IsNa(b)&&a>=b; }
static bool LE(double a,double b){ return !IsNa(a)&&!IsNa(b)&&a<=b; }
static bool PineNeq(double a,double b){ if(IsNa(a)||IsNa(b)) return true; return a!=b; }
static double Nz(double v){ return IsNa(v)?0.0:v; }

// ------------------------------------------------------------------ chart objects
struct Obj {
    std::string type;      // "trend" / "text"
    long   t1=0, t2=0;
    double p1=0, p2=0;
    bool   rayRight=false;
    std::string text;
    bool   above=false;
};
struct Chart {
    std::map<std::string,Obj> objs;
    void deleteAll(const std::string& pfx){
        for(auto it=objs.begin(); it!=objs.end();)
            if(it->first.rfind(pfx,0)==0) it=objs.erase(it); else ++it;
    }
    bool find(const std::string& n) const { return objs.count(n)>0; }
    void del(const std::string& n){ objs.erase(n); }
};
static bool operator==(const Obj&a,const Obj&b){
    return a.type==b.type&&a.t1==b.t1&&a.t2==b.t2&&a.p1==b.p1&&a.p2==b.p2&&
           a.rayRight==b.rayRight&&a.text==b.text&&a.above==b.above;
}

// ------------------------------------------------------------------ indicator
struct Indicator {
    int lb=5, rb=5, maxLines=500;
    bool showSupRes=true, changeBarCol=true;
    std::string prefix="HHLL_";

    // buffers
    std::vector<double> Open,High,Low,Close,Color,HH,HL,LL,LH;
    std::vector<double> Hl,Zz,Res,Sup,Trend,HlChain,ZzChain,ResLine,SupLine;
    // Pine line pool
    std::vector<std::string> lineName; std::vector<int> lineBar;
    Chart chart;

    void resize(int n){
        auto R=[&](std::vector<double>&v){ v.resize(n, 0.0); };
        R(Open);R(High);R(Low);R(Close);R(Color);R(HH);R(HL);R(LL);R(LH);
        R(Hl);R(Zz);R(Res);R(Sup);R(Trend);R(HlChain);R(ZzChain);R(ResLine);R(SupLine);
    }
    void reset(){ chart.objs.clear(); lineName.clear(); lineBar.clear();
                  Open.clear();High.clear();Low.clear();Close.clear();Color.clear();
                  HH.clear();HL.clear();LL.clear();LH.clear();Hl.clear();Zz.clear();
                  Res.clear();Sup.clear();Trend.clear();HlChain.clear();ZzChain.clear();
                  ResLine.clear();SupLine.clear(); }

    double PivotHigh(const std::vector<double>& h,int i) const {
        int p=i-rb; if(p-lb<0) return PINE_NA; double v=h[p];
        for(int k=p-lb;k<p;k++) if(h[k]>=v) return PINE_NA;
        for(int k=p+1;k<=i;k++) if(h[k]>=v) return PINE_NA;
        return v;
    }
    double PivotLow(const std::vector<double>& l,int i) const {
        int p=i-rb; if(p-lb<0) return PINE_NA; double v=l[p];
        for(int k=p-lb;k<p;k++) if(l[k]<=v) return PINE_NA;
        for(int k=p+1;k<=i;k++) if(l[k]<=v) return PINE_NA;
        return v;
    }
    static double ValueWhenPrev(double cur,int p1,int p2,const std::vector<double>& hist){
        if(Truthy(cur)) return p1>=0?hist[p1]:PINE_NA;
        return p2>=0?hist[p2]:PINE_NA;
    }
    bool HistAt(int i,int x,double curHl,double curZz,double&hx,double&zx) const {
        if(x==0){ hx=curHl; zx=curZz; return true; }
        int idx=i-x; if(idx<0) return false;
        hx=Hl[idx]; zx=Zz[idx]; return true;
    }
    void FindPrevious(int i,double curHl,double curZz,
                      double&loc1,double&loc2,double&loc3,double&loc4) const {
        double ehl=(curHl==1.0)?-1.0:1.0;
        loc1=loc2=loc3=loc4=0.0; int xx=0; double hx=0,zx=0;
        for(int x=1;x<=1000;x++) if(HistAt(i,x,curHl,curZz,hx,zx)&&hx==ehl&&!IsNa(zx)){loc1=zx;xx=x+1;break;}
        ehl=curHl;
        for(int x=xx;x<=1000;x++) if(HistAt(i,x,curHl,curZz,hx,zx)&&hx==ehl&&!IsNa(zx)){loc2=zx;xx=x+1;break;}
        ehl=(curHl==1.0)?-1.0:1.0;
        for(int x=xx;x<=1000;x++) if(HistAt(i,x,curHl,curZz,hx,zx)&&hx==ehl&&!IsNa(zx)){loc3=zx;xx=x+1;break;}
        ehl=curHl;
        for(int x=xx;x<=1000;x++) if(HistAt(i,x,curHl,curZz,hx,zx)&&hx==ehl&&!IsNa(zx)){loc4=zx;break;}
    }
    std::string SRLineName(bool isRes,int bar) const {
        return prefix+(isRes?"R":"S")+std::to_string(bar);
    }
    void RegisterLine(const std::string& name,int bar){
        int n=(int)lineName.size();
        for(int k=n-1;k>=0;k--){ if(lineBar[k]<bar) break; if(lineName[k]==name) return; }
        lineName.push_back(name); lineBar.push_back(bar);
        int over=(int)lineName.size()-maxLines;
        if(over>0){ for(int k=0;k<over;k++) chart.del(lineName[k]);
                    lineName.erase(lineName.begin(),lineName.begin()+over);
                    lineBar.erase(lineBar.begin(),lineBar.begin()+over); }
    }
    void TruncateLines(int fromBar){
        for(int k=(int)lineBar.size()-1;k>=0;k--){
            if(lineBar[k]<fromBar) break;
            chart.del(lineName[k]);
            lineName.erase(lineName.begin()+k); lineBar.erase(lineBar.begin()+k);
        }
    }
    void CreateSRLine(bool isRes,int bar,long t1,long t2,double price){
        std::string name=SRLineName(isRes,bar);
        Obj&o=chart.objs[name];
        o.type="trend"; o.t1=t1; o.p1=price; o.t2=t2; o.p2=price; o.rayRight=true;
        RegisterLine(name,bar);
    }
    void FinishSRLine(bool isRes,int bar,long t2){
        std::string name=SRLineName(isRes,bar);
        if(!chart.find(name)) return;
        chart.objs[name].t2=t2; chart.objs[name].rayRight=false;
    }
    void ReopenSRLine(bool isRes,int bar,long t2){
        std::string name=SRLineName(isRes,bar);
        if(!chart.find(name)) return;
        chart.objs[name].t2=t2;
        chart.objs[name].rayRight=true;
    }
    void DrawLabel(int signalBar,long t,double price,const std::string& txt,bool above){
        std::string name=prefix+"L"+std::to_string(signalBar);
        Obj&o=chart.objs[name]; o.type="text"; o.t1=t; o.p1=price; o.text=txt; o.above=above;
    }

    // ---------------------------------------------------------- OnCalculate
    int OnCalculate(int rates_total,int prev_calculated,
                    const std::vector<long>& time,const std::vector<double>& open,
                    const std::vector<double>& high,const std::vector<double>& low,
                    const std::vector<double>& close)
    {
        if(rates_total<lb+rb+2) return 0;
        resize(rates_total);
        int start;
        if(prev_calculated<=0||prev_calculated>rates_total){
            chart.deleteAll(prefix); lineName.clear(); lineBar.clear(); start=0;
        } else {
            start=std::max(0,prev_calculated-1);
            TruncateLines(start);
            for(int j=start;j<rates_total;j++) chart.del(prefix+"L"+std::to_string(j));
            if(start>0){
                int rl=(int)ResLine[start-1]; if(rl>=0) ReopenSRLine(true,rl,time[rl]);
                int sl=(int)SupLine[start-1]; if(sl>=0) ReopenSRLine(false,sl,time[sl]);
            }
        }
        for(int i=start;i<rates_total;i++){
            HH[i]=HL[i]=LL[i]=LH[i]=PINE_NA;
            double ph=PivotHigh(high,i), pl=PivotLow(low,i);
            int hlP1=(i>0)?(int)HlChain[i-1]:-1;
            int hlP2=(hlP1>0)?(int)HlChain[hlP1-1]:-1;
            int zzP1=(i>0)?(int)ZzChain[i-1]:-1;
            int zzP2=(zzP1>0)?(int)ZzChain[zzP1-1]:-1;

            double hl=Truthy(ph)?1.0:(Truthy(pl)?-1.0:PINE_NA);
            double zz=Truthy(ph)?ph:(Truthy(pl)?pl:PINE_NA);

            if(Truthy(pl)&&EQ(hl,-1.0)&&EQ(ValueWhenPrev(hl,hlP1,hlP2,Hl),-1.0)
               &&GT(pl,ValueWhenPrev(zz,zzP1,zzP2,Zz))) zz=PINE_NA;
            if(Truthy(ph)&&EQ(hl,1.0)&&EQ(ValueWhenPrev(hl,hlP1,hlP2,Hl),1.0)
               &&LT(ph,ValueWhenPrev(zz,zzP1,zzP2,Zz))) zz=PINE_NA;
            if(EQ(hl,-1.0)&&EQ(ValueWhenPrev(hl,hlP1,hlP2,Hl),1.0)
               &&GT(zz,ValueWhenPrev(zz,zzP1,zzP2,Zz))) hl=PINE_NA;
            if(EQ(hl,1.0)&&EQ(ValueWhenPrev(hl,hlP1,hlP2,Hl),-1.0)
               &&LT(zz,ValueWhenPrev(zz,zzP1,zzP2,Zz))) hl=PINE_NA;
            if(IsNa(hl)) zz=PINE_NA;

            double a=PINE_NA,b=PINE_NA,c=PINE_NA,d=PINE_NA,e=PINE_NA;
            if(!IsNa(hl)){ double l1,l2,l3,l4; FindPrevious(i,hl,zz,l1,l2,l3,l4);
                           a=zz;b=l1;c=l2;d=l3;e=l4; }

            bool _hh=Truthy(zz)&&(GT(a,b)&&GT(a,c)&&GT(c,b)&&GT(c,d));
            bool _ll=Truthy(zz)&&(LT(a,b)&&LT(a,c)&&LT(c,b)&&LT(c,d));
            bool _hl=Truthy(zz)&&((GE(a,c)&&(GT(b,c)&&GT(b,d)&&GT(d,c)&&GT(d,e)))||(LT(a,b)&&GT(a,c)&&LT(b,d)));
            bool _lh=Truthy(zz)&&((LE(a,c)&&(LT(b,c)&&LT(b,d)&&LT(d,c)&&LT(d,e)))||(GT(a,b)&&LT(a,c)&&GT(b,d)));

            int p=i-rb;
            if(p>=0){
                HH[p]=_hh?zz:PINE_NA; HL[p]=_hl?zz:PINE_NA;
                LL[p]=_ll?zz:PINE_NA; LH[p]=_lh?zz:PINE_NA;
                if(_hh)      DrawLabel(i,time[p],high[p],"HH",true);
                else if(_hl) DrawLabel(i,time[p],low[p], "HL",false);
                else if(_ll) DrawLabel(i,time[p],low[p], "LL",false);
                else if(_lh) DrawLabel(i,time[p],high[p],"LH",true);
            }

            double prevRes=(i>0)?Res[i-1]:PINE_NA;
            double prevSup=(i>0)?Sup[i-1]:PINE_NA;
            double res=_lh?zz:prevRes;
            double sup=_hl?zz:prevSup;
            double trend;
            if(GT(close[i],res)) trend=1.0;
            else if(LT(close[i],sup)) trend=-1.0;
            else trend=(i>0)?Nz(Trend[i-1]):0.0;
            if((trend==1.0&&_hh)||(trend==-1.0&&_lh)) res=zz;
            if((trend==1.0&&_hl)||(trend==-1.0&&_ll)) sup=zz;

            bool rechange=PineNeq(res,prevRes), suchange=PineNeq(sup,prevSup);
            int curResLine=(i>0)?(int)ResLine[i-1]:-1;
            int curSupLine=(i>0)?(int)SupLine[i-1]:-1;
            if(showSupRes){
                long t1=time[std::max(0,i-rb)];
                if(rechange&&!IsNa(res)){
                    if(curResLine>=0) FinishSRLine(true,curResLine,time[i]);
                    CreateSRLine(true,i,t1,time[i],res); curResLine=i;
                }
                if(suchange&&!IsNa(sup)){
                    if(curSupLine>=0) FinishSRLine(false,curSupLine,time[i]);
                    CreateSRLine(false,i,t1,time[i],sup); curSupLine=i;
                }
            }
            if(changeBarCol){ Open[i]=open[i];High[i]=high[i];Low[i]=low[i];Close[i]=close[i];
                              Color[i]=(trend==1.0)?0.0:1.0; }
            else { Open[i]=High[i]=Low[i]=Close[i]=PINE_NA; Color[i]=0.0; }

            Hl[i]=hl; Zz[i]=zz; Res[i]=res; Sup[i]=sup; Trend[i]=trend;
            ResLine[i]=curResLine; SupLine[i]=curSupLine;
            HlChain[i]=Truthy(hl)?(double)i:((i>0)?HlChain[i-1]:-1.0);
            ZzChain[i]=Truthy(zz)?(double)i:((i>0)?ZzChain[i-1]:-1.0);
        }
        return rates_total;
    }
};

// =============================================================== the ORACLE
// A literal, unoptimised transcription of the Pine source: every valuewhen()
// re-scans the whole history, findprevious() walks bar by bar, nothing is
// cached between bars.  Used to prove the optimised version above agrees.
struct Oracle {
    int lb,rb;
    std::vector<double> hl,zz,res,sup,trend;
    std::vector<int> hhB,hlB,llB,lhB;            // per signal bar: 0/1
    std::vector<std::pair<int,double>> resEvents,supEvents; // (bar, level) on change

    static double vw(const std::vector<double>& hist,double cur,int i,int occ){
        int found=0;
        if(Truthy(cur)){ if(found==occ) return cur; found++; }
        for(int k=i-1;k>=0;k--) if(Truthy(hist[k])){ if(found==occ) return hist[k]; found++; }
        return PINE_NA;
    }
    void run(const std::vector<double>&high,const std::vector<double>&low,
             const std::vector<double>&close){
        int n=(int)close.size();
        hl.assign(n,PINE_NA); zz.assign(n,PINE_NA); res.assign(n,PINE_NA);
        sup.assign(n,PINE_NA); trend.assign(n,0.0);
        hhB.assign(n,0); hlB.assign(n,0); llB.assign(n,0); lhB.assign(n,0);
        resEvents.clear(); supEvents.clear();
        for(int i=0;i<n;i++){
            double ph=PINE_NA,pl=PINE_NA;
            { int p=i-rb;
              if(p-lb>=0){ double v=high[p]; bool ok=true;
                for(int k=p-lb;k<=i&&ok;k++) if(k!=p&&high[k]>=v) ok=false;
                if(ok) ph=v; }
              if(p-lb>=0){ double v=low[p]; bool ok=true;
                for(int k=p-lb;k<=i&&ok;k++) if(k!=p&&low[k]<=v) ok=false;
                if(ok) pl=v; } }
            double h_=Truthy(ph)?1.0:(Truthy(pl)?-1.0:PINE_NA);
            double z_=Truthy(ph)?ph:(Truthy(pl)?pl:PINE_NA);
            if(Truthy(pl)&&EQ(h_,-1.0)&&EQ(vw(hl,h_,i,1),-1.0)&&GT(pl,vw(zz,z_,i,1))) z_=PINE_NA;
            if(Truthy(ph)&&EQ(h_,1.0)&&EQ(vw(hl,h_,i,1),1.0)&&LT(ph,vw(zz,z_,i,1))) z_=PINE_NA;
            if(EQ(h_,-1.0)&&EQ(vw(hl,h_,i,1),1.0)&&GT(z_,vw(zz,z_,i,1))) h_=PINE_NA;
            if(EQ(h_,1.0)&&EQ(vw(hl,h_,i,1),-1.0)&&LT(z_,vw(zz,z_,i,1))) h_=PINE_NA;
            if(IsNa(h_)) z_=PINE_NA;

            double a=PINE_NA,b=PINE_NA,c=PINE_NA,d=PINE_NA,e=PINE_NA;
            if(!IsNa(h_)){
                auto at=[&](int x,double&hx,double&zx)->bool{
                    if(x==0){hx=h_;zx=z_;return true;}
                    int id=i-x; if(id<0) return false; hx=hl[id]; zx=zz[id]; return true; };
                double ehl=(h_==1.0)?-1.0:1.0,l1=0,l2=0,l3=0,l4=0,hx,zx; int xx=0;
                for(int x=1;x<=1000;x++) if(at(x,hx,zx)&&hx==ehl&&!IsNa(zx)){l1=zx;xx=x+1;break;}
                ehl=h_;
                for(int x=xx;x<=1000;x++) if(at(x,hx,zx)&&hx==ehl&&!IsNa(zx)){l2=zx;xx=x+1;break;}
                ehl=(h_==1.0)?-1.0:1.0;
                for(int x=xx;x<=1000;x++) if(at(x,hx,zx)&&hx==ehl&&!IsNa(zx)){l3=zx;xx=x+1;break;}
                ehl=h_;
                for(int x=xx;x<=1000;x++) if(at(x,hx,zx)&&hx==ehl&&!IsNa(zx)){l4=zx;break;}
                a=z_;b=l1;c=l2;d=l3;e=l4;
            }
            bool _hh=Truthy(z_)&&(GT(a,b)&&GT(a,c)&&GT(c,b)&&GT(c,d));
            bool _ll=Truthy(z_)&&(LT(a,b)&&LT(a,c)&&LT(c,b)&&LT(c,d));
            bool _hl=Truthy(z_)&&((GE(a,c)&&(GT(b,c)&&GT(b,d)&&GT(d,c)&&GT(d,e)))||(LT(a,b)&&GT(a,c)&&LT(b,d)));
            bool _lh=Truthy(z_)&&((LE(a,c)&&(LT(b,c)&&LT(b,d)&&LT(d,c)&&LT(d,e)))||(GT(a,b)&&LT(a,c)&&GT(b,d)));
            hhB[i]=_hh;hlB[i]=_hl;llB[i]=_ll;lhB[i]=_lh;

            double pr=(i>0)?res[i-1]:PINE_NA, ps=(i>0)?sup[i-1]:PINE_NA;
            double r_=_lh?z_:pr, s_=_hl?z_:ps, t_;
            if(GT(close[i],r_)) t_=1.0; else if(LT(close[i],s_)) t_=-1.0;
            else t_=(i>0)?Nz(trend[i-1]):0.0;
            if((t_==1.0&&_hh)||(t_==-1.0&&_lh)) r_=z_;
            if((t_==1.0&&_hl)||(t_==-1.0&&_ll)) s_=z_;
            if(PineNeq(r_,pr)&&!IsNa(r_)) resEvents.push_back({i,r_});
            if(PineNeq(s_,ps)&&!IsNa(s_)) supEvents.push_back({i,s_});
            hl[i]=h_;zz[i]=z_;res[i]=r_;sup[i]=s_;trend[i]=t_;
        }
    }
};

// =============================================================== test data
struct Series { std::vector<long> t; std::vector<double> o,h,l,c; };

static Series makeSeries(int n,unsigned seed,bool roundTicks){
    std::mt19937 rng(seed);
    std::normal_distribution<double> step(0.0,1.0);
    std::uniform_real_distribution<double> rng01(0.0,1.0);
    Series s; double px=100.0;
    for(int i=0;i<n;i++){
        double o=px, cl=px+step(rng);
        double hi=std::max(o,cl)+rng01(rng)*0.8, lo=std::min(o,cl)-rng01(rng)*0.8;
        if(roundTicks){ // force plenty of ties to exercise the strict pivot rules
            auto r=[&](double v){ return std::round(v*2.0)/2.0; };
            o=r(o);cl=r(cl);hi=r(hi);lo=r(lo);
            hi=std::max({hi,o,cl}); lo=std::min({lo,o,cl});
        }
        s.t.push_back(1700000000L+i*3600L);
        s.o.push_back(o);s.h.push_back(hi);s.l.push_back(lo);s.c.push_back(cl);
        px=cl;
    }
    return s;
}

static int failures=0;
static void check(bool cond,const std::string& what){
    if(!cond){ printf("  FAIL: %s\n",what.c_str()); failures++; }
}
static bool sameD(double a,double b){ return (IsNa(a)&&IsNa(b))||a==b; }

int main(){
    for(unsigned seed=1; seed<=12; seed++){
        bool ties = (seed%3==0);
        Series s = makeSeries(400,seed,ties);
        int n=(int)s.c.size();

        // ---- 1. optimised (full pass) vs literal oracle
        Indicator full; full.lb=5; full.rb=5;
        full.OnCalculate(n,0,s.t,s.o,s.h,s.l,s.c);
        Oracle ora; ora.lb=5; ora.rb=5; ora.run(s.h,s.l,s.c);
        for(int i=0;i<n;i++){
            check(sameD(full.Hl[i],ora.hl[i]),   "hl["+std::to_string(i)+"] seed "+std::to_string(seed));
            check(sameD(full.Zz[i],ora.zz[i]),   "zz["+std::to_string(i)+"] seed "+std::to_string(seed));
            check(sameD(full.Res[i],ora.res[i]), "res["+std::to_string(i)+"] seed "+std::to_string(seed));
            check(sameD(full.Sup[i],ora.sup[i]), "sup["+std::to_string(i)+"] seed "+std::to_string(seed));
            check(full.Trend[i]==ora.trend[i],   "trend["+std::to_string(i)+"] seed "+std::to_string(seed));
            int p=i-5;
            if(p>=0){
                check((!IsNa(full.HH[p]))==(bool)ora.hhB[i],"HH bar "+std::to_string(i));
                check((!IsNa(full.HL[p]))==(bool)ora.hlB[i],"HL bar "+std::to_string(i));
                check((!IsNa(full.LL[p]))==(bool)ora.llB[i],"LL bar "+std::to_string(i));
                check((!IsNa(full.LH[p]))==(bool)ora.lhB[i],"LH bar "+std::to_string(i));
            }
        }

        // ---- 2. incremental (bar by bar, like the terminal) vs full pass
        Indicator inc; inc.lb=5; inc.rb=5;
        int prev=0;
        for(int bars=12; bars<=n; bars++){
            std::vector<long> t(s.t.begin(),s.t.begin()+bars);
            std::vector<double> o(s.o.begin(),s.o.begin()+bars),
                                h(s.h.begin(),s.h.begin()+bars),
                                l(s.l.begin(),s.l.begin()+bars),
                                c(s.c.begin(),s.c.begin()+bars);
            // a couple of in-progress ticks on the forming bar first
            for(int tick=0;tick<2;tick++){
                std::vector<double> o2=o,h2=h,l2=l,c2=c;
                double mid=(h[bars-1]+l[bars-1])/2.0;
                h2[bars-1]=mid+(h[bars-1]-mid)*0.3*(tick+1);
                l2[bars-1]=mid-(mid-l[bars-1])*0.3*(tick+1);
                c2[bars-1]=mid;
                h2[bars-1]=std::max(h2[bars-1],std::max(o2[bars-1],c2[bars-1]));
                l2[bars-1]=std::min(l2[bars-1],std::min(o2[bars-1],c2[bars-1]));
                prev=inc.OnCalculate(bars,prev,t,o2,h2,l2,c2);
            }
            prev=inc.OnCalculate(bars,prev,t,o,h,l,c);   // bar closes
        }
        for(int i=0;i<n;i++){
            check(sameD(inc.Hl[i],full.Hl[i]),   "incremental hl["+std::to_string(i)+"] seed "+std::to_string(seed));
            check(sameD(inc.Zz[i],full.Zz[i]),   "incremental zz["+std::to_string(i)+"] seed "+std::to_string(seed));
            check(sameD(inc.Res[i],full.Res[i]), "incremental res["+std::to_string(i)+"] seed "+std::to_string(seed));
            check(sameD(inc.Sup[i],full.Sup[i]), "incremental sup["+std::to_string(i)+"] seed "+std::to_string(seed));
            check(inc.Trend[i]==full.Trend[i],   "incremental trend["+std::to_string(i)+"] seed "+std::to_string(seed));
            check(sameD(inc.HH[i],full.HH[i]),   "incremental HH["+std::to_string(i)+"] seed "+std::to_string(seed));
            check(sameD(inc.HL[i],full.HL[i]),   "incremental HL["+std::to_string(i)+"] seed "+std::to_string(seed));
            check(sameD(inc.LL[i],full.LL[i]),   "incremental LL["+std::to_string(i)+"] seed "+std::to_string(seed));
            check(sameD(inc.LH[i],full.LH[i]),   "incremental LH["+std::to_string(i)+"] seed "+std::to_string(seed));
        }
        // ---- 3. the chart objects must be identical too
        check(inc.chart.objs.size()==full.chart.objs.size(),
              "object count "+std::to_string(inc.chart.objs.size())+" vs "+
              std::to_string(full.chart.objs.size())+" seed "+std::to_string(seed));
        for(auto&kv:full.chart.objs){
            auto it=inc.chart.objs.find(kv.first);
            if(it==inc.chart.objs.end()){ check(false,"missing object "+kv.first+" seed "+std::to_string(seed)); continue; }
            if(!(it->second==kv.second)){
                printf("  DIFF %s: inc(t1=%ld t2=%ld p1=%g ray=%d) full(t1=%ld t2=%ld p1=%g ray=%d)\n",
                       kv.first.c_str(),it->second.t1,it->second.t2,it->second.p1,(int)it->second.rayRight,
                       kv.second.t1,kv.second.t2,kv.second.p1,(int)kv.second.rayRight);
            }
            check(it->second==kv.second,"object differs "+kv.first+" seed "+std::to_string(seed));
        }
        // ---- 4. the lines that were opened must match the oracle's change events
        int resLines=0,supLines=0;
        for(auto&kv:full.chart.objs){
            if(kv.first.rfind(full.prefix+"R",0)==0) resLines++;
            if(kv.first.rfind(full.prefix+"S",0)==0) supLines++;
        }
        check(resLines==(int)ora.resEvents.size(),"resistance line count seed "+std::to_string(seed));
        check(supLines==(int)ora.supEvents.size(),"support line count seed "+std::to_string(seed));
        for(auto&ev:ora.resEvents){
            std::string nm=full.prefix+"R"+std::to_string(ev.first);
            check(full.chart.find(nm),"missing res line at bar "+std::to_string(ev.first));
            if(full.chart.find(nm)){
                check(full.chart.objs[nm].p1==ev.second,"res level at bar "+std::to_string(ev.first));
                check(full.chart.objs[nm].t1==s.t[std::max(0,ev.first-5)],"res x1 at bar "+std::to_string(ev.first));
            }
        }
        for(auto&ev:ora.supEvents){
            std::string nm=full.prefix+"S"+std::to_string(ev.first);
            check(full.chart.find(nm),"missing sup line at bar "+std::to_string(ev.first));
            if(full.chart.find(nm)) check(full.chart.objs[nm].p1==ev.second,"sup level at bar "+std::to_string(ev.first));
        }
        // ---- 5. exactly one line of each kind may still be extending to the right
        int openRes=0,openSup=0;
        for(auto&kv:full.chart.objs){
            if(kv.second.type!="trend"||!kv.second.rayRight) continue;
            if(kv.first.rfind(full.prefix+"R",0)==0) openRes++; else openSup++;
        }
        check(openRes<=1,"more than one open resistance line, seed "+std::to_string(seed));
        check(openSup<=1,"more than one open support line, seed "+std::to_string(seed));
        // ---- 6. structure sanity: a bar may carry at most one of HH/HL/LL/LH
        for(int i=0;i<n;i++){
            int k=(!IsNa(full.HH[i]))+(!IsNa(full.HL[i]))+(!IsNa(full.LL[i]))+(!IsNa(full.LH[i]));
            check(k<=1,"two labels on bar "+std::to_string(i)+" seed "+std::to_string(seed));
        }
        // ---- 7. every non-na zz must sit on a real pivot high/low
        for(int i=0;i<n;i++){
            if(IsNa(full.Zz[i])) continue;
            int p=i-5;
            check(p>=0&&(full.Zz[i]==s.h[p]||full.Zz[i]==s.l[p]),
                  "zz not on its pivot bar, i="+std::to_string(i)+" seed "+std::to_string(seed));
            check(EQ(full.Hl[i],1.0)?full.Zz[i]==s.h[p]:full.Zz[i]==s.l[p],
                  "zz/hl direction mismatch i="+std::to_string(i));
        }
        // ---- 8. max_lines_count is honoured
        Indicator cap; cap.lb=5; cap.rb=5; cap.maxLines=8;
        cap.OnCalculate(n,0,s.t,s.o,s.h,s.l,s.c);
        int lines=0; for(auto&kv:cap.chart.objs) if(kv.second.type=="trend") lines++;
        check(lines<=8,"max lines exceeded: "+std::to_string(lines)+" seed "+std::to_string(seed));
    }

    // ---- 8b. asymmetric / minimal left-right bar settings
    for(unsigned seed=20; seed<=25; seed++){
        Series s = makeSeries(300,seed,(seed%2==0));
        int n=(int)s.c.size();
        int combos[][2] = {{1,1},{8,2},{2,8},{3,7},{10,1},{1,10}};
        for(auto& cb : combos){
            int LB=cb[0], RB=cb[1];
            Indicator full; full.lb=LB; full.rb=RB;
            full.OnCalculate(n,0,s.t,s.o,s.h,s.l,s.c);
            Oracle ora; ora.lb=LB; ora.rb=RB; ora.run(s.h,s.l,s.c);
            std::string tag=" lb="+std::to_string(LB)+" rb="+std::to_string(RB)+" seed "+std::to_string(seed);
            for(int i=0;i<n;i++){
                check(sameD(full.Hl[i],ora.hl[i]),  "asym hl"+tag);
                check(sameD(full.Zz[i],ora.zz[i]),  "asym zz"+tag);
                check(sameD(full.Res[i],ora.res[i]),"asym res"+tag);
                check(sameD(full.Sup[i],ora.sup[i]),"asym sup"+tag);
                check(full.Trend[i]==ora.trend[i],  "asym trend"+tag);
                int p=i-RB;
                if(p>=0){
                    check((!IsNa(full.HH[p]))==(bool)ora.hhB[i],"asym HH"+tag);
                    check((!IsNa(full.HL[p]))==(bool)ora.hlB[i],"asym HL"+tag);
                    check((!IsNa(full.LL[p]))==(bool)ora.llB[i],"asym LL"+tag);
                    check((!IsNa(full.LH[p]))==(bool)ora.lhB[i],"asym LH"+tag);
                }
            }
            Indicator inc; inc.lb=LB; inc.rb=RB;
            int prev=0;
            for(int bars=LB+RB+2; bars<=n; bars++){
                std::vector<long> t(s.t.begin(),s.t.begin()+bars);
                std::vector<double> o(s.o.begin(),s.o.begin()+bars),h(s.h.begin(),s.h.begin()+bars),
                                    l(s.l.begin(),s.l.begin()+bars),c(s.c.begin(),s.c.begin()+bars);
                std::vector<double> c2=c; c2[bars-1]=(h[bars-1]+l[bars-1])/2.0;
                prev=inc.OnCalculate(bars,prev,t,o,h,l,c2);   // forming bar
                prev=inc.OnCalculate(bars,prev,t,o,h,l,c);    // bar closes
            }
            for(int i=0;i<n;i++){
                check(sameD(inc.Zz[i],full.Zz[i]),  "asym incremental zz"+tag);
                check(sameD(inc.Res[i],full.Res[i]),"asym incremental res"+tag);
                check(sameD(inc.Sup[i],full.Sup[i]),"asym incremental sup"+tag);
                check(inc.Trend[i]==full.Trend[i],  "asym incremental trend"+tag);
                check(sameD(inc.HH[i],full.HH[i]),  "asym incremental HH"+tag);
                check(sameD(inc.LH[i],full.LH[i]),  "asym incremental LH"+tag);
            }
            check(inc.chart.objs.size()==full.chart.objs.size(),"asym object count"+tag);
            for(auto&kv:full.chart.objs){
                auto it=inc.chart.objs.find(kv.first);
                if(it==inc.chart.objs.end()){ check(false,"asym missing object "+kv.first+tag); continue; }
                check(it->second==kv.second,"asym object differs "+kv.first+tag);
            }
        }
    }

    // ---- 9. a hand built series: clean zig-zag, structure must be HH/HL/...
    {
        //             idx: 0..  a rising staircase of swings
        std::vector<double> mid={100,101,102,103,104,105,104,103,102,101,102,103,104,105,
                                 106,107,108,109,110,109,108,107,106,105,106,107,108,109,
                                 110,111,112,113,114,115,114,113,112,111,110,111,112,113};
        Series s; for(size_t i=0;i<mid.size();i++){
            s.t.push_back(1700000000L+(long)i*3600L);
            s.o.push_back(mid[i]); s.c.push_back(mid[i]);
            s.h.push_back(mid[i]+0.1); s.l.push_back(mid[i]-0.1);
        }
        Indicator ind; ind.lb=3; ind.rb=3;
        ind.OnCalculate((int)mid.size(),0,s.t,s.o,s.h,s.l,s.c);
        std::string seq;
        for(size_t i=0;i<mid.size();i++){
            if(!IsNa(ind.HH[i])) seq+="HH ";
            if(!IsNa(ind.HL[i])) seq+="HL ";
            if(!IsNa(ind.LL[i])) seq+="LL ";
            if(!IsNa(ind.LH[i])) seq+="LH ";
        }
        printf("  staircase structure: %s\n",seq.c_str());
        check(seq.find("LL")==std::string::npos,"lower low on a rising staircase");
        check(seq.find("LH")==std::string::npos,"lower high on a rising staircase");
        check(seq.find("HH")!=std::string::npos,"no higher high on a rising staircase");
    }

    if(failures==0) printf("ALL CHECKS PASSED\n");
    else            printf("%d CHECK(S) FAILED\n",failures);
    return failures==0?0:1;
}
