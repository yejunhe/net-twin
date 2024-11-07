# 功能

1 从param.json读取：
	路由器列表
	对应接口信息
	cost值
	NE40路由器telnet登录信息
	frr路由器 dockerid 作为登录信息
2.1 针对NE40类型路由器，telnet连接，执行相关指令，修改cost值
2.2 针对frr类型路由器，执行docker 指令连接，执行相关指令，修改cost值
3 将修改cost的结果信息输入到 data.txt
4 增加了有关bgp路由反射读不出来的情况，添加了有关display bgp all summary的输出结果
#使用
python3 /root/net-twin/smart/ospf_cost_frrne40.py -i /uploadPath/reasoning/{t}/params/param.json -o /uploadPath/reasoning/{t}/res/data.txt

若-i -o不包含占位符{t}，则按照指定的路径输入、输出。