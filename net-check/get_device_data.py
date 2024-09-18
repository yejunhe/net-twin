#!/usr/bin/env python3


import pymysql

def create_connection_to_mysql(host, password):
    connection = pymysql.connect(host = host, user = "librenms", password = password, database = 'librenms', charset='utf8mb4', cursorclass=pymysql.cursors.DictCursor)
    cursor = connection.cursor()
    return connection, cursor

def execute_sql(connection, cursor, sql_str, is_insert):
    exec_result = None
    cursor.execute(sql_str)
    if is_insert:
        connection.commit()
    else:
        exec_result = cursor.fetchall()
    
    return exec_result

#if __name__ == "__main__":
#    connection, cursor = create_connection_to_mysql("172.16.204.161", "librenms@2024")
#    sql = "SELECT ipv4_address, ipv4_prefixlen FROM ipv4_addresses;"
#    result = execute_sql(connection, cursor, sql, False)
#    print("result = ", result)

