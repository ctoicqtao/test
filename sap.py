# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "mcp[cli]",
#     "requests",
#     "pydantic>=2.0",
#     "xmltodict",
# ]
# ///

import os
import requests
import xmltodict
from typing import List, Optional
from mcp.server.fastmcp import FastMCP, Context
from pydantic import Field
import threading
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache

# ==============================================================================
# Configuration
# ==============================================================================
class SAPConfig:
    # 线程池配置
    MAX_WORKERS = int(os.environ.get("SAP_MAX_WORKERS", "100"))  # 最大并发线程数
    REQUEST_TIMEOUT = int(os.environ.get("SAP_REQUEST_TIMEOUT", "300"))  # 请求超时时间（秒）
    
    SERVICES = {
        "SO": {
            "url": "https://vhivcqasci.sap.inventec.com:44300/sap/bc/srt/rfc/sap/zws_bapi_salesorder_create/100/zws_bapi_salesorder_create_sev/zws_bapi_salesorder_create_binding",
            "action": '"urn:sap-com:document:sap:rfc:functions:ZWS_BAPI_SALESORDER_CREATE:ZBAPI_SALESORDER_CREATERequest"'
        },
        "STO": {
            "url": "https://vhivcqasci.sap.inventec.com:44300/sap/bc/srt/rfc/sap/zsd_sto_create/100/zsd_sto_create_svr/zsd_sto_create_binding",
            "action": '"urn:sap-com:document:sap:rfc:functions:ZSD_STO_CREATE:ZSD_STO_CREATERequest"'
        },
        "DN": {
            "url": "https://vhivcqasci.sap.inventec.com:44300/sap/bc/srt/rfc/sap/zws_bapi_outb_delivery_create/100/zws_bapi_outb_delivery_create/bind_dn_create",
            "action": '"urn:sap-com:document:sap:rfc:functions:ZWS_BAPI_OUTB_DELIVERY_CREATE_STO:ZBAPI_OUTB_DELIVERY_CREATE_STORequest"'
        },
        "MAT": {
            "url": "https://vhivcqasci.sap.inventec.com:44300/sap/bc/srt/rfc/sap/zws_bapi_material_savedata/100/zws_bapi_material_savedata/bind_material",
            "action": '"urn:sap-com:document:sap:rfc:functions:ZWS_BAPI_MATERIAL_SAVEDATA:ZBAPI_MATERIAL_SAVEDATARequest"'
        },
        "SRC": {
            "url": "https://vhivcqasci.sap.inventec.com:44300/sap/bc/srt/rfc/sap/zsd_source_list_maintain/100/zsd_source_list_maintain_svr/zsd_source_list_maintain_binding",
            "action": '"urn:sap-com:document:sap:rfc:functions:ZSD_SOURCE_LIST_MAINTAIN:ZSD_SOURCE_LIST_MAINTAINRequest"'
        },
        "INF": {
            "url": "https://vhivcqasci.sap.inventec.com:44300/sap/bc/srt/rfc/sap/zws_info_record_maintain/100/zws_info_record_maintain_svr/zws_info_record_maintain_binding",
            "action": '"urn:sap-com:document:sap:rfc:functions:ZWS_INFO_RECORD_MAINTAIN:ZSD_INFO_RECORD_MAINTAINRequest"'
        },
        "QTY": {
            "url": "https://vhivcqasci.sap.inventec.com:44300/sap/bc/srt/rfc/sap/zsd_kitting_flow_change/100/zsd_kitting_flow_change_svr/zsd_kitting_flow_change_bind",
            "action": '"urn:sap-com:document:sap:rfc:functions:ZSD_KITTING_FLOW_CHANGE:ZSD_KITTING_FLOW_CHANGERequest"'
        }
    }

# ==============================================================================
# Session Management
# ==============================================================================
class SessionCredentialStore:
    """存储不同 MCP session 的 SAP 凭据（线程安全）"""
    def __init__(self):
        # 使用字典存储：session_id -> {user, password}
        self._credentials = {}
        # 添加线程锁保护共享资源
        self._lock = threading.RLock()

        # 如果有环境变量，作为默认凭据
        default_user = os.environ.get("SAP_USER")
        default_password = os.environ.get("SAP_PASSWORD")
        if default_user and default_password:
            self._default_credentials = {
                "user": default_user,
                "password": default_password
            }
        else:
            self._default_credentials = None

    def set_credentials(self, session_id: str, user: str, password: str):
        """为指定 session 设置凭据（线程安全）"""
        with self._lock:
            self._credentials[session_id] = {
                "user": user,
                "password": password
            }

    def get_credentials(self, session_id: str):
        """获取指定 session 的凭据（线程安全）"""
        with self._lock:
            # 优先使用 session 特定的凭据
            if session_id in self._credentials:
                # 返回副本以避免外部修改
                return self._credentials[session_id].copy()

            # 如果没有，尝试使用默认凭据
            if self._default_credentials:
                return self._default_credentials.copy()

            raise ValueError(f"未找到 session {session_id} 的凭据，且未设置默认凭据。请先调用 set_sap_credentials 设置凭据。")

    def has_credentials(self, session_id: str) -> bool:
        """检查是否有凭据（线程安全）"""
        with self._lock:
            return session_id in self._credentials or self._default_credentials is not None

    def clear_credentials(self, session_id: str):
        """清除指定 session 的凭据（线程安全）"""
        with self._lock:
            if session_id in self._credentials:
                del self._credentials[session_id]

# 全局凭据存储
credential_store = SessionCredentialStore()

# ==============================================================================
# Core Client
# ==============================================================================
mcp = FastMCP("SAP Automation Agent")

# 全局线程池，用于处理并发请求
_thread_pool = ThreadPoolExecutor(max_workers=SAPConfig.MAX_WORKERS)

class SAPClient:
    # 为每个线程创建独立的 Session 对象，提高性能
    _thread_local = threading.local()
    
    def __init__(self, key: str, session_id: str):
        cfg = SAPConfig.SERVICES[key]
        self.url = cfg["url"]
        self.action = cfg["action"]

        # 从凭据存储中获取此 session 的凭据
        creds = credential_store.get_credentials(session_id)
        self.user = creds["user"]
        self.password = creds["password"]

    @classmethod
    def _get_session(cls) -> requests.Session:
        """获取线程本地的 Session 对象（线程安全）"""
        if not hasattr(cls._thread_local, 'session'):
            cls._thread_local.session = requests.Session()
            # 配置连接池
            adapter = requests.adapters.HTTPAdapter(
                pool_connections=10,
                pool_maxsize=20,
                max_retries=3
            )
            cls._thread_local.session.mount('https://', adapter)
            cls._thread_local.session.mount('http://', adapter)
        return cls._thread_local.session

    def post_soap(self, body_content: str) -> str:
        """发送 SOAP 请求（线程安全）"""
        # Standard SOAP Envelope without XML declaration
        envelope = f'<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/" xmlns:urn="urn:sap-com:document:sap:rfc:functions"><soapenv:Header/><soapenv:Body>{body_content}</soapenv:Body></soapenv:Envelope>'

        headers = {
            'Content-Type': 'text/xml; charset=utf-8',
            'Accept': 'text/xml',
            'SOAPAction': self.action,
        }

        try:
            # 使用线程本地的 Session
            session = self._get_session()
            response = session.post(
                self.url,
                data=envelope.encode('utf-8'),
                auth=(self.user, self.password),
                headers=headers,
                verify=False,
                timeout=SAPConfig.REQUEST_TIMEOUT
            )

            if response.status_code == 200:
                try:
                    parsed = xmltodict.parse(response.text)
                    # Try to extract Body content
                    env = parsed.get('soap-env:Envelope') or parsed.get('soapenv:Envelope') or parsed.get('SOAP-ENV:Envelope')
                    if env:
                        body = env.get('soap-env:Body') or env.get('soapenv:Body') or env.get('SOAP-ENV:Body')
                        return str(body) if body else response.text
                    return response.text
                except:
                    return response.text
            else:
                return f"HTTP Error {response.status_code}: {response.text}"

        except requests.exceptions.Timeout:
            return f"请求超时：超过 {SAPConfig.REQUEST_TIMEOUT} 秒未响应"
        except Exception as e:
            return f"连接错误: {str(e)}"

# ==============================================================================
# Session Management Tools
# ==============================================================================

@mcp.tool()
def set_sap_credentials(
    username: str,
    password: str,
    ctx: Context = None
) -> str:
    """设置当前会话的 SAP 凭据

    参数:
        username: SAP 用户名
        password: SAP 密码

    返回:
        设置结果消息
    """
    if ctx and hasattr(ctx, 'request_context'):
        # 使用 request_id 作为 session 标识
        session_id = getattr(ctx.request_context, 'request_id', 'default')
        # 更好的做法是使用 session 信息，但这里用 client_params 模拟
        if hasattr(ctx, 'session') and hasattr(ctx.session, 'client_params'):
            client_info = str(ctx.session.client_params)
            session_id = hash(client_info) if client_info else 'default'
        session_id = str(session_id)
    else:
        session_id = 'default'

    credential_store.set_credentials(session_id, username, password)
    return f"已为会话 {session_id} 设置 SAP 凭据（用户：{username}）"

@mcp.tool()
def check_session_credentials(ctx: Context = None) -> str:
    """检查当前会话是否已设置凭据

    返回:
        凭据状态信息
    """
    if ctx and hasattr(ctx, 'request_context'):
        session_id = getattr(ctx.request_context, 'request_id', 'default')
        if hasattr(ctx, 'session') and hasattr(ctx.session, 'client_params'):
            client_info = str(ctx.session.client_params)
            session_id = hash(client_info) if client_info else 'default'
        session_id = str(session_id)
    else:
        session_id = 'default'

    if credential_store.has_credentials(session_id):
        creds = credential_store.get_credentials(session_id)
        return f"会话 {session_id} 已设置凭据（用户：{creds['user']}）"
    else:
        return f"会话 {session_id} 未设置凭据"

def _get_session_id(ctx: Context = None) -> str:
    """从 Context 中提取 session ID"""
    if ctx and hasattr(ctx, 'request_context'):
        session_id = getattr(ctx.request_context, 'request_id', 'default')
        if hasattr(ctx, 'session') and hasattr(ctx.session, 'client_params'):
            client_info = str(ctx.session.client_params)
            session_id = hash(client_info) if client_info else 'default'
        return str(session_id)
    return 'default'

# ==============================================================================
# SAP Operation Tools
# ==============================================================================

@mcp.tool()
def create_sales_order(
    CUST_PO: str,
    CUST_PO_DATE: str,
    MATERIAL: str,
    QTY: float,
    UUID: str = "",
    ORDER_TYPE: str = "ZIES",
    SALES_ORG: str = "TW01",
    SALES_CHANNEL: str = "03",
    SALES_DIVISION: str = "01",
    SOLD_TO_PARTY: str = "HRCTO-IMX",
    SHIP_TO_PARTY: str = "HRCTO-MX",
    PLANT: str = "TP01",
    SHIPPING_POINT: str = "TW01",
    ctx: Context = None
) -> str:
    """步骤 1: 创建销售订单"""

    # 获取当前会话的 session ID
    session_id = _get_session_id(ctx)

    # Enforce defaults to prevent 'Mandatory header fields missing'
    order_type_val = ORDER_TYPE if ORDER_TYPE else "ZIES"
    sales_org_val = SALES_ORG if SALES_ORG else "TW01"
    sales_channel_val = SALES_CHANNEL if SALES_CHANNEL else "03"
    sales_division_val = SALES_DIVISION if SALES_DIVISION else "01"
    sold_to_val = SOLD_TO_PARTY if SOLD_TO_PARTY else "HRCTO-IMX"
    ship_to_val = SHIP_TO_PARTY if SHIP_TO_PARTY else "HRCTO-MX"
    plant_val = PLANT if PLANT else "TP01"
    shipping_pt_val = SHIPPING_POINT if SHIPPING_POINT else "TW01"

    cust_po_val = CUST_PO if CUST_PO else "TEST_PO"
    cust_po_date_val = CUST_PO_DATE if CUST_PO_DATE else "2025-01-01"

    uuid_tag = f"<UUID>{UUID}</UUID>" if UUID else ""

    # Structure strictly following Word doc: UUID -> CUST -> ITEM TABLE -> HEADER FIELDS
    xml_body = f'<urn:ZBAPI_SALESORDER_CREATE>{uuid_tag}<CUST_PO>{cust_po_val}</CUST_PO><CUST_PO_DATE>{cust_po_date_val}</CUST_PO_DATE><IT_SO_ITEM><item><MATERIAL_NO>000010</MATERIAL_NO><MATERIAL>{MATERIAL}</MATERIAL><UNIT>PCE</UNIT><QTY>{QTY}</QTY><PLANT>{plant_val}</PLANT><SHIPPING_POINT>{shipping_pt_val}</SHIPPING_POINT><DELIVERY_DATE>{cust_po_date_val}</DELIVERY_DATE></item></IT_SO_ITEM><ORDER_TYPE>{order_type_val}</ORDER_TYPE><SALES_CHANNEL>{sales_channel_val}</SALES_CHANNEL><SALES_DIVISION>{sales_division_val}</SALES_DIVISION><SALES_ORG>{sales_org_val}</SALES_ORG><SHIP_TO_PARTY>{ship_to_val}</SHIP_TO_PARTY><SOLD_TO_PARTY>{sold_to_val}</SOLD_TO_PARTY></urn:ZBAPI_SALESORDER_CREATE>'

    return SAPClient("SO", session_id).post_soap(xml_body)

@mcp.tool()
def create_sto_po(
    PR_NUMBER: str,
    PR_ITEM: str,
    UUID: str = "",
    PUR_GROUP: str = "999",
    PUR_ORG: str = "TW10",
    PUR_PLANT: str = "TP01",
    VENDOR: str = "ICC-CP60",
    DOC_TYPE: str = "NB",
    ctx: Context = None
) -> str:
    """步骤 2: 创建 STO PO"""

    session_id = _get_session_id(ctx)

    pur_group_val = PUR_GROUP if PUR_GROUP else "999"
    pur_org_val = PUR_ORG if PUR_ORG else "TW10"
    pur_plant_val = PUR_PLANT if PUR_PLANT else "TP01"
    vendor_val = VENDOR if VENDOR else "ICC-CP60"
    doc_type_val = DOC_TYPE if DOC_TYPE else "NB"

    uuid_tag = f"<UUID>{UUID}</UUID>" if UUID else ""

    xml_body = f'<urn:ZSD_STO_CREATE>{uuid_tag}<DOC_TYPE>{doc_type_val}</DOC_TYPE><LGORT/><PR_NUMBER>{PR_NUMBER}</PR_NUMBER><PUR_GROUP>{pur_group_val}</PUR_GROUP><PUR_ITEM><item><BNFPO>{PR_ITEM}</BNFPO></item></PUR_ITEM><PUR_ORG>{pur_org_val}</PUR_ORG><PUR_PLANT>{pur_plant_val}</PUR_PLANT><VENDOR>{vendor_val}</VENDOR></urn:ZSD_STO_CREATE>'

    return SAPClient("STO", session_id).post_soap(xml_body)

@mcp.tool()
def create_outbound_delivery(
    PO_NUMBER: str,
    ITEM_NO: str,
    QUANTITY: float,
    UUID: str = "",
    ctx: Context = None
) -> str:
    """步骤 3: 创建出库交货单"""

    session_id = _get_session_id(ctx)

    ship_point_val = "CN60"
    uuid_tag = f"<UUID>{UUID}</UUID>" if UUID else ""

    xml_body = f'<urn:ZBAPI_OUTB_DELIVERY_CREATE_STO>{uuid_tag}<PO_ITEM><item><REF_DOC>{PO_NUMBER}</REF_DOC><REF_ITEM>{ITEM_NO}</REF_ITEM><DLV_QTY>{QUANTITY}</DLV_QTY><SALES_UNIT>EA</SALES_UNIT></item></PO_ITEM><SHIP_POINT>{ship_point_val}</SHIP_POINT></urn:ZBAPI_OUTB_DELIVERY_CREATE_STO>'

    return SAPClient("DN", session_id).post_soap(xml_body)

@mcp.tool()
def maintain_info_record(
    MATERIAL: str,
    UUID: str = "",
    PRICE: str = "999",
    VENDOR: str = "ICC-CP60",
    PLANT: str = "TP01",
    PUR_ORG: str = "TW10",
    ctx: Context = None
) -> str:
    """补救操作: 维护信息记录"""

    session_id = _get_session_id(ctx)

    price_val = PRICE if PRICE else "999"
    vendor_val = VENDOR if VENDOR else "ICC-CP60"
    plant_val = PLANT if PLANT else "TP01"
    pur_org_val = PUR_ORG if PUR_ORG else "TW10"

    uuid_tag = f"<UUID>{UUID}</UUID>" if UUID else ""

    xml_body = f'<urn:ZSD_INFO_RECORD_MAINTAIN>{uuid_tag}<CURRENCY>USD</CURRENCY><MATERIAL>{MATERIAL}</MATERIAL><PLANT>{plant_val}</PLANT><PRICE>{price_val}</PRICE><PRICE_UNIT>1</PRICE_UNIT><PUR_ORG>{pur_org_val}</PUR_ORG><VENDOR>{vendor_val}</VENDOR></urn:ZSD_INFO_RECORD_MAINTAIN>'

    return SAPClient("INF", session_id).post_soap(xml_body)

@mcp.tool()
def maintain_sales_view(
    MATERIAL: str,
    SALES_ORG: str,
    DISTR_CHAN: str,
    UUID: str = "",
    PLANT: str = "TP01",
    DELYG_PLNT: str = "TP01",
    ctx: Context = None
) -> str:
    """补救操作: 维护销售视图"""

    session_id = _get_session_id(ctx)

    # Logic from Word Doc
    plant_val = PLANT
    delyg_plnt_val = DELYG_PLNT

    if SALES_ORG == "CN60" and DISTR_CHAN == "03":
        plant_val = "CP60"
        delyg_plnt_val = "CP60"
    elif SALES_ORG == "TW01" and DISTR_CHAN == "03":
        plant_val = "TP01"
        delyg_plnt_val = "TP01"

    plant_val = plant_val if plant_val else "TP01"
    delyg_plnt_val = delyg_plnt_val if delyg_plnt_val else "TP01"

    uuid_tag = f"<UUID>{UUID}</UUID>" if UUID else ""

    xml_body = f'<urn:ZBAPI_MATERIAL_SAVEDATA>{uuid_tag}<HEADDATA><MATERIAL>{MATERIAL}</MATERIAL><SALES_VIEW>X</SALES_VIEW><STORAGE_VIEW></STORAGE_VIEW><WAREHOUSE_VIEW></WAREHOUSE_VIEW></HEADDATA><PLANTDATA><PLANT>{plant_val}</PLANT></PLANTDATA><SALESDATA><SALES_ORG>{SALES_ORG}</SALES_ORG><DISTR_CHAN>{DISTR_CHAN}</DISTR_CHAN><DELYG_PLNT>{delyg_plnt_val}</DELYG_PLNT></SALESDATA></urn:ZBAPI_MATERIAL_SAVEDATA>'

    return SAPClient("MAT", session_id).post_soap(xml_body)

@mcp.tool()
def maintain_warehouse_view(
    MATERIAL: str,
    UUID: str = "",
    WHSE_NO: str = "WH1",
    ctx: Context = None
) -> str:
    """补救操作: 维护仓库视图"""

    session_id = _get_session_id(ctx)

    whse_no_val = WHSE_NO if WHSE_NO else "WH1"
    uuid_tag = f"<UUID>{UUID}</UUID>" if UUID else ""

    xml_body = f'<urn:ZBAPI_MATERIAL_SAVEDATA>{uuid_tag}<HEADDATA><MATERIAL>{MATERIAL}</MATERIAL><SALES_VIEW></SALES_VIEW><STORAGE_VIEW></STORAGE_VIEW><WAREHOUSE_VIEW>X</WAREHOUSE_VIEW></HEADDATA><WAREHOUSENUMBERDATA><WHSE_NO>{whse_no_val}</WHSE_NO></WAREHOUSENUMBERDATA></urn:ZBAPI_MATERIAL_SAVEDATA>'

    return SAPClient("MAT", session_id).post_soap(xml_body)

@mcp.tool()
def maintain_source_list(
    MATERIAL: str,
    VALID_FROM: str,
    UUID: str = "",
    PLANT: str = "TP01",
    VENDOR: str = "ICC-CP60",
    ctx: Context = None
) -> str:
    """补救操作: 维护源清单"""

    session_id = _get_session_id(ctx)

    plant_val = PLANT if PLANT else "TP01"
    vendor_val = VENDOR if VENDOR else "ICC-CP60"
    valid_from_val = VALID_FROM if VALID_FROM else "2025-01-01"
    uuid_tag = f"<UUID>{UUID}</UUID>" if UUID else ""

    xml_body = f'<urn:ZSD_SOURCE_LIST_MAINTAIN>{uuid_tag}<MATERIAL>{MATERIAL}</MATERIAL><PLANT>{plant_val}</PLANT><VENDOR>{vendor_val}</VENDOR><VALID_FROM>{valid_from_val}</VALID_FROM><VALID_TO>9999-12-31</VALID_TO></urn:ZSD_SOURCE_LIST_MAINTAIN>'

    return SAPClient("SRC", session_id).post_soap(xml_body)

@mcp.tool()
def change_kitting_qty(
    KITTING_PO: str,
    PO_ITEM: str,
    QUANTITY: float,
    UUID: str = "",
    ctx: Context = None
) -> str:
    """补救操作: 更改 Kitting PO 数量"""

    session_id = _get_session_id(ctx)

    uuid_tag = f"<UUID>{UUID}</UUID>" if UUID else ""

    xml_body = f'<urn:ZSD_KITTING_FLOW_CHANGE>{uuid_tag}<KITTING_PO>{KITTING_PO}</KITTING_PO><PR_ITEM><item><EBELP>{PO_ITEM}</EBELP><MENGE>{QUANTITY}</MENGE></item></PR_ITEM></urn:ZSD_KITTING_FLOW_CHANGE>'

    return SAPClient("QTY", session_id).post_soap(xml_body)

# ==============================================================================
# 批量操作工具（利用多线程）
# ==============================================================================

@mcp.tool()
def batch_create_sales_orders(
    orders: List[dict],
    ctx: Context = None
) -> str:
    """批量创建销售订单（并发执行）
    
    参数:
        orders: 订单列表，每个订单是一个包含以下字段的字典:
            - CUST_PO: 客户采购订单号
            - CUST_PO_DATE: 订单日期
            - MATERIAL: 物料编号
            - QTY: 数量
            - UUID: (可选) 唯一标识符
            其他字段均为可选，使用默认值
    
    返回:
        JSON 格式的结果列表
    """
    import json
    
    session_id = _get_session_id(ctx)
    
    def create_single_order(order_data: dict) -> dict:
        """创建单个订单的辅助函数"""
        try:
            result = create_sales_order(
                CUST_PO=order_data.get("CUST_PO", ""),
                CUST_PO_DATE=order_data.get("CUST_PO_DATE", ""),
                MATERIAL=order_data.get("MATERIAL", ""),
                QTY=order_data.get("QTY", 0),
                UUID=order_data.get("UUID", ""),
                ORDER_TYPE=order_data.get("ORDER_TYPE", "ZIES"),
                SALES_ORG=order_data.get("SALES_ORG", "TW01"),
                SALES_CHANNEL=order_data.get("SALES_CHANNEL", "03"),
                SALES_DIVISION=order_data.get("SALES_DIVISION", "01"),
                SOLD_TO_PARTY=order_data.get("SOLD_TO_PARTY", "HRCTO-IMX"),
                SHIP_TO_PARTY=order_data.get("SHIP_TO_PARTY", "HRCTO-MX"),
                PLANT=order_data.get("PLANT", "TP01"),
                SHIPPING_POINT=order_data.get("SHIPPING_POINT", "TW01"),
                ctx=ctx
            )
            return {
                "success": True,
                "order": order_data.get("CUST_PO", "unknown"),
                "result": result
            }
        except Exception as e:
            return {
                "success": False,
                "order": order_data.get("CUST_PO", "unknown"),
                "error": str(e)
            }
    
    # 使用线程池并发执行
    futures = [_thread_pool.submit(create_single_order, order) for order in orders]
    results = [future.result() for future in futures]
    
    return json.dumps(results, ensure_ascii=False, indent=2)

@mcp.tool()
def get_thread_pool_status() -> str:
    """获取线程池状态信息
    
    返回:
        线程池的当前状态
    """
    import json
    
    status = {
        "max_workers": SAPConfig.MAX_WORKERS,
        "request_timeout": SAPConfig.REQUEST_TIMEOUT,
        "thread_pool_type": "ThreadPoolExecutor",
        "description": "服务器使用线程池处理并发请求，提高吞吐量"
    }
    
    return json.dumps(status, ensure_ascii=False, indent=2)

# ==============================================================================
# 服务器启动
# ==============================================================================

if __name__ == "__main__":
    # 禁用 SSL 警告（因为使用 verify=False）
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    
    print("=" * 60)
    print("SAP Automation Agent - 多线程版本")
    print("=" * 60)
    print(f"最大并发线程数: {SAPConfig.MAX_WORKERS}")
    print(f"请求超时时间: {SAPConfig.REQUEST_TIMEOUT} 秒")
    print("=" * 60)
    print()
    print("环境变量配置:")
    print("  SAP_MAX_WORKERS: 设置最大并发线程数 (默认: 10)")
    print("  SAP_REQUEST_TIMEOUT: 设置请求超时时间/秒 (默认: 30)")
    print("  SAP_USER: 设置默认 SAP 用户名")
    print("  SAP_PASSWORD: 设置默认 SAP 密码")
    print("=" * 60)
    print()
    
    mcp.run()
