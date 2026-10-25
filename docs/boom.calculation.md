| &nbsp; 	|&nbsp;| 
| :--- 	 	| :--- |
|夜间hrv基线 hrv_night.avg|      睡眠期间RMSSD均值的7天滚动平均|
|夜间hrv标准差  hrv_night.std|      睡眠期间RMSSD均值的7天标准差|
|清晨hrv基线  hrv_morning.avg|    清晨hrv定义：识别到睡眠转变为清醒状态后，记录早晨刚醒阶段的RMSSD均值（测量条件中断条件：持续时间＞5min 或 状态再次转为睡眠 或 持续5s心率相较开始测试的前5s平均心率增加超过20bpm ）清晨hrv基线（hrv_morning.avg）定义：清晨hrv的7天滚动平均|
|清晨hrv标准差hrv_morning.std| 清晨hrv的7天标准差|
|入睡时间基线bed_time.avg|    每晚记录到第一次睡眠状态的时间，以7天滚动平均作为基线|
|静息心率基线restHeartRate.avg|   静息心率过去7天的滚动平均|
|静息心率标准差restHeartRate.std|   静息心率过去7天的标准差|
|非运动、非睡眠状态心率基线  non_SportSleepHR.avg|	过去7天，用户在非睡眠、非运动状态下心率的均值|
|非运动、非睡眠状态心率标准差   non_SportSleepHR.std|	过去7天，用户在非睡眠、非运动状态下心率的标准差|
|最大心率HR_max|	初始值220-年龄；当运动过程中持续5s记录到心率数值大于此前保存的数值，则更新HR_max|



基准值计算
需要记录下列指标的基准值与标准差来完成算法的相关计算：


1.状态healthStatus.status
| &nbsp; 	|&nbsp;| 
| :--- 	 	| :--- |
|状态healthStatus.status|     显示当前数值；每小时更新|
|心率变异性hrv|	显示当前数值；显示过去5min的RMSSD均值|
|静息心率restingHeartRate|	显示当天采集到的数值|
|当日HRV变化|	平均：5min的RMSSD均值生成的曲线范围：60min的RMSSD均值±标准差|
|训练准备度|	取每天最高的状态值生成对应日期的柱状图|


healthStatus.status状态计算详细说明:
按照下面的计算方式，每天早晨睡眠统计完成后计算状态得分；之后每小时根据动态条件项公式对状态得分进行更新修正。
1. 基础分50，算分公式 = 50 / weight，其中权重weight矩阵如下：
在基础分的计算中，一个滚动周期内hrv越稳定、波动越小，则状态分值就越高。每行代表hrv_night.std / hrv_night.avg；列代表hrv_morning.std / hrv_morning.avg。
| &nbsp; 	|&nbsp;| &nbsp; | &nbsp; 	|  &nbsp; | 
| :--- 	 	| :--- | :--- 	| :--- 		| :--- |
|	        |<5%	    |5%~10%	|10%~15%	    |>15%|
|<5%	        |1	    |1.5	    |2	        |2.5|
|5%~10%	    |1.5	    |2	    |2.5	        |3|
|10%~15%	    |2	    |2.5	    |3	        |3.5|
|>15%	    |2.5	    |3	    |3.5	        |4|



2. 睡眠分50，算分公式 = 50 ÷ weight，其中权重weight矩阵如下：
| &nbsp; 	|&nbsp;| &nbsp; | &nbsp; 	|  &nbsp; | 
| :--- 	 	| :--- | :--- 	| :--- 		| :--- |
|			|>8h	|6.5~8h	|5~6.5h		|<5h|
|<30min		|1		|1.5	|2			|2.5|
|30min~60min|	1.5	|2	|2.5	|3|
|60min~90min|	2	|2.5	|3	|3.5|
|>90min|	|2.5	|3	|3.5	|4|

每行代表每晚睡眠时长；每列代表设备记录到入睡时间比建议入睡时间晚的时间（建议入睡时间 = 前7天平均入睡时间 - 1小时）。

3. 扣分项
| &nbsp; 	|&nbsp;| 
| :--- 	 	| :--- |
|-5分| 静息心率高于基线（restHeartRate > restHeartRate.avg）、睡眠期间RMSSD均值低于基线(hrv_night < hrv_night.avg)、清晨hrv低于基线(hrv_morning < hrv morning.avg)、spO2 < 95%；|
|-10分| 静息心率高于基线+1标准差(restHeartRate > restHeartRate.avg + restHeartRate.std)、睡眠期间RMSSD均值低于基线-1标准差(hrv_night < hrv_night.avg - hrv_night.std)、清晨hrv低于基线-1标准差(hrv_morning < hrv morning.avg - hrv morning.std)、spO2 < 90%；|

4. 加分项
| &nbsp; 	|&nbsp;| 
| :--- 	 	| :--- |
|+5分| 静息心率低于基线（restHeartRate < restHeartRate.avg）、睡眠期间RMSSD均值高于基线(hrv_night > hrv_night.avg)、清晨hrv高于基线(hrv_morning > hrv morning.avg)；|
|+10分|静息心率低于基线-1标准差(restHeartRate < restHeartRate.avg - restHeartRate.std)、睡眠期间RMSSD均值高于基线+1标准差(hrv_night > hrv_night.avg + hrv_night.std)、清晨hrv高于基线+1标准差(hrv_morning > hrv morning.avg + hrv morning.std)；|

5. 动态调节项

对高负荷、高生活压力下的身体状态进行动态修正。如果用户没有主动打开app查看，默认每小时更新一次计算；如果用户打开app界面，则以打开app时刻进行一次更新计算
休息/高压状态判定：
每一次更新计算状态分数，以当天早晨的状态分数为基准加上动态调节分。统计从得到早晨状态分数的时刻到更新状态分数这段时间内的下列信息：
1.心率低于基线的时长t1，
2.心率高于基线的时长t2，
3.心率低于基线 - 标准差的时长t3，
4.心率高于基线 + 标准差的时长t4 
 备注：t1-t4的时间单位为1小时
动态调节分 = 5 ×（t1 - t2）+ 10 ×（t3 - t4） 
 
综上healthStatus.status = 基础分 + 睡眠分 + 扣分项 + 加分项 + 动态调节，并且以1小时为单位进行动态更新。
PS:后续会对矩阵界限参数设置进行优化，并且通过统计学方法将权重矩阵修正为连续的。

6. 前7天未建立基线情况下的状态计算
	临时算法仅用于正式7天基线建立前，目标是计算简单、结果直观。只进行固定档位判断和加减分，不计算临时均值、标准差或权重矩阵。
* 首日分数
	第1个有效日早晨的临时状态分T_1固定为60分，不根据单日HRV或静息心率推断个人状态。当天后续状态仅按（6.3）的即时调节项更新。
* 第2～6个有效日的早晨分数
	从第2个有效日起，以前一个有效日的早晨临时状态分为起点，根据当天数据做固定加减分：
	T_d = clamp(T_(d-1) + Δ_sleep + Δ_SpO2 + Δ_RHR + Δ_HRV, 30, 90)。
	其中clamp表示将临时分限制在30～90分，避免基线尚未建立时出现极端结果。各调整项如下：
	睡眠时长Δ_sleep：≥8h加5分；6.5～8h为0分；5～6.5h扣5分；<5h扣10分。
	血氧Δ_SpO2：SpO2 ≥95%为0分；90%≤SpO2<95%扣5分；SpO2<90%扣10分。
	静息心率Δ_RHR：与前一个有效日相比，下降≥5bpm加3分；上升≥5bpm扣3分；变化不足5bpm为0分。
	夜间HRV调整Δ_HRV：与前一个有效日相比，上升≥10%加3分；下降≥10%扣3分；变化不足10%为0分。
	某项当天或前一个有效日缺少数据时，该项记0分，不用0值替代原始数据。每天只保存早晨临时分T_d作为下一天的起点，小时更新后的分数不得带入次日，避免重复累计。
* 当天小时更新
	小时更新不建立心率基线，仅将当前非运动、非睡眠状态下最近5min平均心率HR_5min与当天静息心率RHR_today比较：
	HR_5min ≤ RHR_today + 20bpm，Δ_hour=0；RHR_today + 20bpm < HR_5min ≤ RHR_today + 40bpm，Δ_hour=-2；HR_5min > RHR_today + 40bpm，Δ_hour=-5。
	当前状态分 = clamp(T_d + Δ_hour, 30, 90)。每次打开App或每小时重新计算一次，均以当天早晨分T_d为起点，不累加上一小时的扣分。处于运动、睡眠状态或心率缺失时，Δ_hour取0。
* 正式算法切换
	第7次有效睡眠统计完成后的早晨，如已具备正式算法所需的7个有效日数据，停止使用临时算法，直接按前文正式算法计算healthStatus.status；若第7日仍因缺测无法形成正式算法所需的7个有效日，则继续使用临时算法，直至7个有效日齐全后再切换。有效日计数仅判断是否切换，不参与临时分数计算。


7. 负荷计算说明totalLoad

总负荷totalLoad计算：当实时心率HR < 45%(HR_max - HR_rest) + HR_rest时，计算当前非运动状态下带来的负荷。公式如下：
S1 = sum(t_i * 0.6 * (HR- HR_rest)/ 45% / (HR_max - HR_rest) * exp((HR - HR_rest)/45% / (HR_max - HR_rest)))
         
HR为实时心率；
HR_max为最大心率，默认为220-年龄（若用户未输入年龄，则HR_max = 190），如果用户佩戴产品过程中非异常值的心率超过当下储存的HR_max则将HR_max进行更新；
HR_rest静息心率，取过去7天静息心率基线代入；
t_i，以每分钟为单位，HR用1分钟平均心率代替，通过累加求和对S1进行计算。
当实时心率HR > 45%(HR_max - HR_rest) + HR_rest时，计算当前运动状态下带来的负荷。运动负荷tss计算公式如下：
tss = sum(t_i * 0.4 * (HR - HR_rest) / 45% / (HR_max - HR_rest) * exp(1.92 * (HR - HR_rest) / 45% / (HR_max - HR_rest)) / 60)
         
如果是女子，则tss计算公式修正为：
tss = sum(t_i * 0.5 * (HR - HR_rest) / 45% / (HR_max - HR_rest) * exp(1.64 * (HR - HR_rest) / 45% / (HR_max - HR_rest)) / 60)

总负荷totalLoad = S1 + tss
实时负荷曲线：每分钟按照上面的公式对负荷进行累加，曲线图为负荷随时间增长的关系
负荷趋势：每天总负荷的统计
运动负荷：每天运动负荷的统计

8. 储能totalEnergy计算说明
储能totalEnergy用于表示用户在当天累计负荷作用后剩余的可用能量。算法使用0～1的内部状态递推，产品端显示为0～100分。该指标是训练与恢复管理中的状态指数，不等同于以焦耳计量的生理能量。
* 数据与参数说明
totalLoad_(d,t)：第d天截至时刻t的累计总负荷，按前文公式计算，totalLoad = S1 + tss。
E_d：第d天早晨、尚未承受当天负荷时的起始储能，范围0～1。首次启用算法时E_1=1；以后由前一天结束时的剩余储能恢复得到。
F_(d,t)：第d天截至时刻t，在累计负荷作用后的剩余储能。内部计算范围限制为-0.1～1，负值用于保留极端透支信息。
T_d：第d天身体状态修正因子。正常状态为1；清晨HRV显著低于个人基线时大于1。当天T_d固定，不随小时更新。
S_(d,t)：由累计总负荷totalLoad_(d,t)经Sigmoid函数转换得到的标准化压力，范围0～1。
固定参数：消耗系数k=0.5；Sigmoid中点m=125、尺度s=45；基础恢复时间常数τ_base=1.5，低储能时τ_effective=3.0。
（3.2）身体状态修正因子T_d
每天早晨读取清晨HRV（hrv_morning）及其7天基线和标准差，按下面的方式确定T_d：

T_d = 1 when HRVd >= HRVavg - HRVstd
T_d = max(1, (HRVavg - HRVd) / HRVstd) when HRVd < HRVavg - HRVstd

其中HRV_avg使用hrv_morning.avg，HRV_std使用hrv_morning.std。若尚未建立7天HRV基线、清晨HRV缺失或HRV_std≤0，则T_d取1，避免缺失值被误判为身体状态异常。

* 累计负荷标准化
将当天截至当前时刻的totalLoad转换为标准化压力。采用已经验证的参数组合m=125、s=45，并对零负荷设置独立规则：

S_(d,t) = 0 when totalLoad_d_t = 0
S_(d,t) = 1 / (1 + exp(-(totalLoad_d_t - 125) / 45)) when totalLoad_d_t > 0

零点规则保证确认无负荷时不扣减储能。采用（125，45）时，totalLoad为50、100、150、250、300时，S约为0.159、0.365、0.635、0.941、0.980，可同时区分日常负荷与偶发高负荷。

* 当天剩余储能与显示值
当天以早晨起始储能E_d为固定起点，用身体状态修正后的标准化压力计算当前剩余储能：

F_(d,t) = clamp(E_d - 0.5 * T_d * S_(d,t), -0.1, 1)

产品端显示的储能分数为：

totalEnergy = round(100 * clamp(F_(d,t), 0, 1))

totalLoad每分钟累计更新一次，totalEnergy随之重新计算。每次均使用当天固定的E_d和T_d代入当前累计totalLoad，不在上一分钟F_(d,t)的基础上再次扣减，避免同一负荷被重复计算。当天totalLoad=0时，S_(d,t)=0，因此F_(d,t)=E_d。

* 日终结算与次日恢复
以当天最后一个有效时刻的F_(d,t)作为日终剩余储能F_d。当已经有此前7个有效日的F时，计算前7个有效日的均值F_avg和标准差F_std；当天F_d不参与本次阈值计算。有效恢复时间常数为：
t_effective = 3.0 when F_d < F_avg - F_std
t_effective = 1.5 otherwisse

由日终剩余储能F_d计算次日早晨起始储能E_(d+1)：
E_(d+1) = clamp(1 - (1 - F_d) * exp(-1 / t_effective), 0, 1)

t_effective越大，恢复速度越慢。次日睡眠统计完成后，先得到E_(d+1)并作为当天固定起点，再开始累计新的totalLoad。

* 数据缺失与有效日规则
负荷缺失：totalLoad=0只用于确认无负荷的情况；心率或负荷数据缺失时不得用0替代。当天显示保留最近一次有效totalEnergy并标记数据不完整。
HRV缺失：尚未建立7天基线、HRV缺失或标准差无效时T_d=1；HRV恢复有效后，于下一次早晨计算更新T_d。
日终无效：若当天无法得到有效F_d，则该日不进入F的7日滚动均值和标准差，也不执行由该日F_d到次日E的恢复递推；待获得下一有效日数据后继续。

* 完整计算顺序
1. 每天早晨读取E_d；首次启用时E_1=1。
2. 读取清晨HRV及7天基线，计算并固定当天T_d；条件不足时T_d=1。
3. 每分钟累计S1与tss，得到totalLoad_(d,t)=S1+tss。
4. 用m=125、s=45将totalLoad_(d,t)转换为S_(d,t)，确认零负荷时S_(d,t)=0。
5. 由E_d、T_d和S_(d,t)重新计算F_(d,t)，再转换为0～100分的totalEnergy。
6. 日终保存最后一个有效F_d，并用此前7个有效日判断t_effective。
7. 由F_d和t_effective计算E_(d+1)，进入下一天递推。