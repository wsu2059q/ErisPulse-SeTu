from ErisPulse import sdk
import aiohttp
import asyncio
from typing import Dict, List, Optional, Any, Set

class APIConfig:
    def __init__(self, name: str, url: str, supports_tags: bool, supports_pid: bool, supports_author: bool):
        self.name = name
        self.url = url
        self.supports_tags = supports_tags
        self.supports_pid = supports_pid
        self.supports_author = supports_author

class Main:
    def __init__(self):
        self.sdk = sdk
        self.logger = sdk.logger
        self.adapter = sdk.adapter
        self._register_handlers()
        self.config = self._load_config()
        
        self.api_configs = {
            "lolicon": APIConfig(
                name="lolicon",
                url="https://api.lolicon.app/setu/v2",
                supports_tags=True,
                supports_pid=True,
                supports_author=True
            ),
            "mossia": APIConfig(
                name="mossia",
                url="https://api.mossia.top/duckMo",
                supports_tags=False,
                supports_pid=True,
                supports_author=True
            ),
            "anosu": APIConfig(
                name="anosu",
                url="https://image.anosu.top/pixiv/json",
                supports_tags=True,
                supports_pid=False,
                supports_author=False
            )
        }
        
        self.current_api = self.config.get("current_api", "lolicon")
        self.max_retries = self.config.get("max_retries", 10)
        self.r18_pass = self.config.get("r18_pass", False)
        self.search_states: Dict[str, Dict] = {}
        self.timeout = 60
        self._supported_sends_cache: Dict[str, Set[str]] = {}

    def _load_config(self) -> Dict:
        config = self.sdk.env.getConfig("SeTu")
        if not config:
            default_config = {
                "current_api": "lolicon",
                "max_retries": 10,
                "r18_pass": False
            }
            self.sdk.env.setConfig("SeTu", default_config)
            return default_config
        return config

    def _check_send_method(self, adapter_name: str, sender: Any, method: str) -> bool:
        # 优先使用 list_sends 方法
        if hasattr(self.adapter, 'list_sends'):
            try:
                if adapter_name not in self._supported_sends_cache:
                    methods = self.adapter.list_sends(adapter_name)
                    if methods:
                        self._supported_sends_cache[adapter_name] = set(methods)
                    else:
                        self._supported_sends_cache[adapter_name] = set()
                
                return method in self._supported_sends_cache.get(adapter_name, set())
            except Exception as e:
                self.logger.debug(f"list_sends 查询失败，降级使用 hasattr: {e}")
        
        # 降级使用 hasattr
        return hasattr(sender, method)

    @staticmethod
    def should_eager_load() -> bool:
        return True

    def _register_handlers(self):
        self.adapter.on("message")(self._handle_message)
        self.logger.info("色图模块已加载")

    async def _handle_message(self, data: Dict):
        if not data.get("alt_message"):
            return
            
        text = data.get("alt_message", "").strip().lower()
        if text in ["/随机色图", "随机色图", "/色图", "色图"]:
            asyncio.create_task(self._process_image_request(data))
        elif text.startswith("/搜索色图") or text.startswith("搜索色图"):
            asyncio.create_task(self._start_search_interaction(data))
        elif text.startswith("/切换api") or text.startswith("切换api"):
            asyncio.create_task(self._switch_api(data))
        elif text in ["/色图状态面板", "色图状态面板"]:
            asyncio.create_task(self._show_status_panel(data))
        else:
            asyncio.create_task(self._process_search_interaction(data))

    async def _show_status_panel(self, data: Dict):
        current_api = self.api_configs[self.current_api]
        
        status_msg = [
            "=== 色图模块状态面板 ===",
            f"当前API: {self.current_api}",
            f"支持功能:",
            f"  - 标签搜索: {'✓' if current_api.supports_tags else '✗'}",
            f"  - PID搜索: {'✓' if current_api.supports_pid else '✗'}",
            f"  - 作者搜索: {'✓' if current_api.supports_author else '✗'}",
            f"R18内容: {'开启' if self.r18_pass else '关闭'}",
            f"最大重试次数: {self.max_retries}",
            "",
            "可用API列表:",
            *[f"- {name}: 标签{'✓' if cfg.supports_tags else '✗'} | PID{'✓' if cfg.supports_pid else '✗'} | 作者{'✓' if cfg.supports_author else '✗'}" 
              for name, cfg in self.api_configs.items()],
            "",
            "使用'切换api [名称]'来更改当前API"
        ]
        
        await self._send_text_response(data, "\n".join(status_msg))

    async def _switch_api(self, data: Dict):
        text = data.get("alt_message", "").strip().lower()
        parts = text.split()
        
        if len(parts) < 2:
            available_apis = ", ".join(self.api_configs.keys())
            await self._send_text_response(data, f"当前API: {self.current_api}\n可用API: {available_apis}\n请使用'切换api [api名称]'来切换")
            return
            
        new_api = parts[1].lower()
        if new_api not in self.api_configs:
            await self._send_text_response(data, f"无效的API名称，可用选项: {', '.join(self.api_configs.keys())}")
            return
            
        self.current_api = new_api
        self.config["current_api"] = new_api
        self.sdk.env.setConfig("SeTu", self.config)
        await self._send_text_response(data, f"已切换至 {new_api} API")

    async def _start_search_interaction(self, data: Dict):
        user_id = data.get("user_id")
        if user_id in self.search_states:
            await self._send_text_response(data, "您已经有一个进行中的搜索，请先完成或等待超时")
            return
            
        self.search_states[user_id] = {
            "step": "ask_search_type",
            "params": {
                "tag": [],       # 标签搜索参数
                "pid": None,     # PID搜索参数
                "author": None,  # 作者搜索参数
                "r18": 0,        # R18过滤参数
                "num": 1,        # 返回数量，默认1
                "size": ["original"]  # 图片尺寸，默认原图
            },
            "last_active": asyncio.get_event_loop().time()
        }
        
        asyncio.create_task(self._check_interaction_timeout(user_id))
        
        current_api_config = self.api_configs[self.current_api]
        options = []
        
        if current_api_config.supports_tags:
            options.append("1. 按标签搜索")
        if current_api_config.supports_pid:
            options.append("2. 按PID搜索")
        if current_api_config.supports_author:
            options.append("3. 按作者搜索")
            
        if not options:
            await self._send_text_response(data, "当前API不支持任何搜索方式")
            del self.search_states[user_id]
            return
            
        await self._send_text_response(data, f"请选择搜索方式：\n{' '.join(options)}\n\n回复数字选择")

    async def _process_search_interaction(self, data: Dict):
        user_id = data.get("user_id")
        text = data.get("alt_message", "").strip()
        
        if user_id not in self.search_states:
            return
            
        state = self.search_states[user_id]
        state["last_active"] = asyncio.get_event_loop().time()
        current_api_config = self.api_configs[self.current_api]
        
        try:
            if state["step"] == "ask_search_type":
                await self._handle_search_type_step(data, state, text, current_api_config)
                
            elif state["step"] == "ask_tags":
                await self._handle_tags_step(data, state, text, current_api_config)
                
            elif state["step"] == "ask_pid":
                await self._handle_pid_step(data, state, text, current_api_config)
                
            elif state["step"] == "ask_author":
                await self._handle_author_step(data, state, text, current_api_config)
                
            elif state["step"] == "ask_r18":
                await self._handle_r18_step(data, state, text)
                
            elif state["step"] == "ask_num":
                await self._handle_num_step(data, state, text)
                
            elif state["step"] == "select_images":
                await self._handle_select_images_step(data, state, text)
                
        except Exception as e:
            self.logger.error(f"交互搜索出错: {str(e)}")
            await self._send_text_response(data, f"出错了: {str(e)}")
            if user_id in self.search_states:
                del self.search_states[user_id]

    async def _handle_search_type_step(self, data: Dict, state: Dict, text: str, api_config: APIConfig):
        if text == "1" and api_config.supports_tags:
            state["step"] = "ask_tags"
            await self._send_text_response(data, "请输入要搜索的标签，多个标签用空格分隔：")
        elif text == "2" and api_config.supports_pid:
            state["step"] = "ask_pid"
            await self._send_text_response(data, "请输入要搜索的PID，多个PID用空格分隔：")
        elif text == "3" and api_config.supports_author:
            state["step"] = "ask_author"
            await self._send_text_response(data, "请输入作者名称：")
        else:
            await self._send_text_response(data, "请输入有效的数字选项")

    async def _handle_tags_step(self, data: Dict, state: Dict, text: str, api_config: APIConfig):
        if not api_config.supports_tags:
            await self._send_text_response(data, "当前API不支持标签搜索")
            del self.search_states[data.get("user_id")]
            return
            
        tags = text.split()
        if len(tags) > 3:
            await self._send_text_response(data, "最多支持3个标签组合")
            return
            
        state["params"]["tag"] = tags
        state["params"]["r18"] = 0
        state["step"] = "ask_r18"
        await self._send_text_response(data, "是否包含R18内容？(是/否)")

    async def _handle_pid_step(self, data: Dict, state: Dict, text: str, api_config: APIConfig):
        if not api_config.supports_pid:
            await self._send_text_response(data, "当前API不支持PID搜索")
            del self.search_states[data.get("user_id")]
            return
            
        try:
            pids = [int(pid) for pid in text.split()]
            if self.current_api == "lolicon":
                state["params"]["pid"] = pids[0] if pids else None
            else:
                state["params"]["pid"] = pids
                
            state["params"]["r18"] = 0
            state["step"] = "ask_num"
            await self._send_text_response(data, "要返回多少张图片？(1-20)")
        except ValueError:
            await self._send_text_response(data, "PID必须是数字，请重新输入")

    async def _handle_author_step(self, data: Dict, state: Dict, text: str, api_config: APIConfig):
        if not api_config.supports_author:
            await self._send_text_response(data, "当前API不支持作者搜索")
            del self.search_states[data.get("user_id")]
            return
            
        state["params"]["author"] = text
        state["params"]["r18"] = 0
        state["step"] = "ask_r18"
        await self._send_text_response(data, "是否包含R18内容？(是/否)")

    async def _handle_r18_step(self, data: Dict, state: Dict, text: str):
        if text.lower() in ["是", "yes", "y"]:
            if not self.r18_pass:
                await self._send_text_response(data, "小孩子不能看这些哦！看点小孩子该看的捏~")
                state["params"]["r18"] = 0
            else:
                state["params"]["r18"] = 1
        else:
            state["params"]["r18"] = 0
            
        state["step"] = "ask_num"
        await self._send_text_response(data, "要返回多少张图片？(1-20)")

    async def _handle_num_step(self, data: Dict, state: Dict, text: str):
        try:
            num = int(text)
            if num < 1 or num > 20:
                await self._send_text_response(data, "数量必须在1-20之间")
                return
                
            state["params"]["num"] = num
            await self._send_text_response(data, "正在搜索图片，请稍候...")
            
            state["params"]["size"] = ["original"]
            
            asyncio.create_task(self._perform_async_search(data, data.get("user_id"), state))
            
        except ValueError:
            await self._send_text_response(data, "请输入有效的数字")

    async def _handle_select_images_step(self, data: Dict, state: Dict, text: str):
        try:
            selected = [int(x)-1 for x in text.split() if x.isdigit()]
            valid_selected = [x for x in selected if 0 <= x < len(state["results"])]
            
            if not valid_selected:
                await self._send_text_response(data, "没有选择有效的图片")
                del self.search_states[data.get("user_id")]
                return
            
            asyncio.create_task(self._send_selected_images(data, valid_selected, state["results"]))
            del self.search_states[data.get("user_id")]
            
        except Exception as e:
            await self._send_text_response(data, f"发送图片时出错: {str(e)}")
            del self.search_states[data.get("user_id")]

    async def _perform_async_search(self, data: Dict, user_id: str, state: Dict):
        try:
            results = await self._search_images(state["params"])
            if not results:
                await self._send_text_response(data, "没有找到符合条件的图片")
                del self.search_states[user_id]
                return
                
            state["step"] = "select_images"
            state["results"] = results
            
            message = "找到以下图片，请选择要查看的图片(可多选，用空格分隔)：\n"
            for i, img in enumerate(results, 1):
                message += f"{i}. {img['title']} by {img['author']} (PID: {img['pid']})\n"
            await self._send_text_response(data, message)
            
        except Exception as e:
            await self._send_text_response(data, f"搜索失败: {str(e)}")
            if user_id in self.search_states:
                del self.search_states[user_id]

    async def _search_images(self, params: Dict) -> Optional[List[Dict]]:
        api_config = self.api_configs[self.current_api]
        
        if self.current_api == "lolicon":
            return await self._search_lolicon(params)
        elif self.current_api == "mossia":
            return await self._search_mossia(params)
        elif self.current_api == "anosu":
            return await self._search_anosu(params)
        else:
            self.logger.warning(f"未知的API: {self.current_api}")
            return None

    async def _search_lolicon(self, params: Dict) -> Optional[List[Dict]]:
        api_url = self.api_configs["lolicon"].url
        lolicon_params = {
            "r18": params.get("r18", 0),
            "num": params.get("num", 1),
            "size": params.get("size", ["original"])[0]
        }
        
        if "tag" in params:
            lolicon_params["tag"] = params["tag"]
        if "pid" in params and params["pid"]:
            lolicon_params["pid"] = params["pid"]
        if "author" in params:
            lolicon_params["keyword"] = params["author"]
            
        async with aiohttp.ClientSession() as session:
            async with session.get(api_url, params=lolicon_params) as resp:
                if resp.status != 200:
                    self.logger.warning(f"搜索API请求失败({resp.status})")
                    return None
                
                json_data = await resp.json()
                if json_data.get('error'):
                    self.logger.warning(f"搜索API返回错误({json_data.get('message', '未知错误')})")
                    return None
                
                data = json_data.get('data', [])
                return [{
                    "pid": item["pid"],
                    "title": item["title"],
                    "author": item["author"],
                    "urlsList": [{"urlSize": "original", "url": item["urls"]["original"]}]
                } for item in data]
        
    async def _search_anosu(self, params: Dict) -> Optional[List[Dict]]:
        api_url = self.api_configs["anosu"].url
        anosu_params = {
            "num": params.get("num", 1),
            "r18": params.get("r18", 0),
            "proxy": "i.pixiv.re"
        }
        
        if "tag" in params:
            anosu_params["keyword"] = " ".join(params["tag"])
            
        async with aiohttp.ClientSession() as session:
            async with session.get(api_url, params=anosu_params) as resp:
                if resp.status != 200:
                    self.logger.warning(f"anosu API请求失败({resp.status})")
                    return None
                
                json_data = await resp.json()
                if not isinstance(json_data, list):
                    self.logger.warning("anosu API返回数据格式不正确")
                    return None
                    
                return [{
                    "pid": item.get("pid", 0),
                    "title": item.get("title", "无标题"),
                    "author": item.get("author", "未知作者"),
                    "r18": item.get("r18", 0),
                    "urlsList": [{"urlSize": "original", "url": item.get("url", "")}],
                    "tags": item.get("tags", [])
                } for item in json_data]
        
    async def _search_mossia(self, params: Dict) -> Optional[List[Dict]]:
        api_url = self.api_configs["mossia"].url
        mossia_params = {
            "r18Type": params.get("r18", 0),
            "num": params.get("num", 1),
            "sizeList": params.get("size", ["original"])
        }
        
        if "pid" in params:
            mossia_params["pid"] = params["pid"]
        if "author" in params:
            mossia_params["author"] = params["author"]
            
        async with aiohttp.ClientSession() as session:
            async with session.post(api_url, json=mossia_params) as resp:
                if resp.status != 200:
                    self.logger.warning(f"搜索API请求失败({resp.status})")
                    return None
                
                json_data = await resp.json()
                if not json_data.get('success'):
                    self.logger.warning(f"搜索API返回错误({json_data.get('message', '未知错误')})")
                    return None
                
                return json_data.get('data', [])

    async def _send_selected_images(self, data: Dict, selected_indices: List[int], results: List[Dict]):
        adapter_name = data.get("self", {}).get("platform", None)
        sender = await self._get_adapter_sender(data)
        
        has_html = self._check_send_method(adapter_name, sender, 'Html')
        has_markdown = self._check_send_method(adapter_name, sender, 'Markdown')
        has_image = self._check_send_method(adapter_name, sender, 'Image')
        
        for idx in selected_indices:
            img = results[idx]
            image_url = next((u["url"] for u in img["urlsList"] if u["urlSize"] == "original"), None)
            if not image_url:
                continue
                
            try:
                if has_html or has_markdown:
                    if has_html:
                        html_content = f'<img src="{image_url}" />'
                        await sender.Html(html_content)
                    else:
                        markdown_content = f'![]({image_url})'
                        await sender.Markdown(markdown_content)
                    
                elif has_image:
                    # 支持图片发送的平台下载后发送
                    image_data = await self._download_image(image_url)
                    if image_data:
                        await sender.Image(image_data)
                    
                else:
                    # 都不支持的平台发送纯文本URL
                    await sender.Text(f"{img['title']} 图片链接: {image_url}")
                    
                await asyncio.sleep(0.5)
                
            except Exception as e:
                self.logger.error(f"发送图片失败: {str(e)}")
                await self._send_text_response(data, f"图片发送失败: {img['title']}")

    async def _send_text_response(self, data: Dict, text: str):
        adapter_name = data.get("self", {}).get("platform", None)
        sender = await self._get_adapter_sender(data)
        if self._check_send_method(adapter_name, sender, "Text"):
            await sender.Text(text)
        else:
            self.logger.warning(f"无法发送文本消息: {text}")

    async def _send_image_response(self, data: Dict, image_data: bytes):
        adapter_name = data.get("self", {}).get("platform", None)
        sender = await self._get_adapter_sender(data)
        if self._check_send_method(adapter_name, sender, "Image"):
            await sender.Image(image_data)
        else:
            self.logger.warning("平台不支持图片发送")

    async def _check_interaction_timeout(self, user_id: str):
        await asyncio.sleep(self.timeout)
        if user_id in self.search_states:
            last_active = self.search_states[user_id].get("last_active", 0)
            current_time = asyncio.get_event_loop().time()
            if current_time - last_active >= self.timeout:
                del self.search_states[user_id]
                self.logger.info(f"用户 {user_id} 的搜索交互已超时")

    async def _process_image_request(self, data: Dict):
        try:
            adapter_name = data.get("self", {}).get("platform", None)
            sender = await self._get_adapter_sender(data)
            
            has_html = self._check_send_method(adapter_name, sender, 'Html')
            has_markdown = self._check_send_method(adapter_name, sender, 'Markdown')
            has_image = self._check_send_method(adapter_name, sender, 'Image')
            
            if self._check_send_method(adapter_name, sender, "Text"):
                msg_id_data = await sender.Text("收到了喵~正在为您准备图片喵~")
            else:
                self.logger.warning("平台不支持文本发送")
                
            retry_count = 0
            while retry_count < self.max_retries:
                try:
                    image_url = await self._fetch_image_url()
                    if not image_url:
                        retry_count += 1
                        continue

                    if has_html or has_markdown:
                        if has_html:
                            html_content = f'<img src="{image_url}" />'
                            await sender.Html(html_content)
                        else:
                            markdown_content = f'![]({image_url})'
                            await sender.Markdown(markdown_content)
                        
                        self.logger.info("通过HTML/Markdown发送图片URL成功")
                        
                    elif has_image:
                        image_data = await self._download_image(image_url)
                        if not image_data:
                            retry_count += 1
                            continue
                        
                        await sender.Image(image_data)
                        self.logger.info("图片下载并发送成功")
                        
                    else:
                        await sender.Text(f"图片链接: {image_url}")
                        self.logger.info("通过纯文本发送图片URL")
                    
                    if self._check_send_method(adapter_name, sender, "Edit"):
                        await sender.Edit(msg_id_data.get("message_id", ""), "准备好了喵~尽情欣赏吧~")
                    break

                except aiohttp.ClientError as e:
                    if "404" in str(e):
                        retry_count += 1
                        self.logger.warning(f"图片下载404错误，正在重试...({retry_count}/{self.max_retries})")
                        continue
                    await self._send_warning_text(data, f"图片处理失败: {str(e)}")
                    return
                except Exception as e:
                    await self._send_warning_text(data, f"图片处理失败: {str(e)}")
                    return

            if retry_count >= self.max_retries:
                await self._send_warning_text(data, "图片获取失败，已达到最大重试次数")

        except Exception as e:
            self.logger.error(f"图片处理失败: {str(e)}")
            await self._send_warning_text(data, f"图片处理失败: {str(e)}")

    async def _get_adapter_sender(self, data: Dict):
        detail_type = data.get("detail_type", "private")
        datail_id = data.get("user_id") if detail_type == "private" else data.get("group_id")
        adapter_name = data.get("self", {}).get("platform", None)
        
        self.logger.info(f"获取到消息来源: {adapter_name} {detail_type} {datail_id}")
        if not adapter_name:
            self.logger.warning("无法获取消息来源平台")
            
        adapter = getattr(self.sdk.adapter, adapter_name)
        return adapter.Send.To("user" if detail_type == "private" else "group", datail_id)

    async def _send_warning_text(self, data: Dict, text: str):
        adapter_name = data.get("self", {}).get("platform", None)
        sender = await self._get_adapter_sender(data)
        if self._check_send_method(adapter_name, sender, "Text"):
            await sender.Text(text)
            return
        else:
            self.logger.warning(text)
                
    async def _fetch_image_url(self) -> Optional[str]:
        api_config = self.api_configs[self.current_api]
        
        if self.current_api == "lolicon":
            return await self._fetch_lolicon_image()
        elif self.current_api == "mossia":
            return await self._fetch_mossia_image()
        elif self.current_api == "anosu":
            return await self._fetch_anosu_image()
        else:
            self.logger.warning(f"未知的API: {self.current_api}")
            return None

    async def _fetch_lolicon_image(self) -> Optional[str]:
        api_url = self.api_configs["lolicon"].url
        params = {
            "r18": 0,
            "num": 1,
            "size": "original"
        }
        
        async with aiohttp.ClientSession() as session:
            async with session.get(api_url, params=params) as resp:
                if resp.status != 200:
                    self.logger.warning(f"图片URL获取失败({resp.status})")
                    return None
                
                json_data = await resp.json()
                if json_data.get('error'):
                    self.logger.warning(f"图片API返回错误({json_data.get('message', '未知错误')})")
                    return None
                
                if not json_data.get('data') or len(json_data['data']) == 0:
                    self.logger.warning("图片API返回空数据")
                    return None
                
                return json_data['data'][0]['urls']['original']

    async def _fetch_mossia_image(self) -> Optional[str]:
        api_url = self.api_configs["mossia"].url
        params = {
            "sizeList": ["original"],
            "r18Type": 0
        }
        
        async with aiohttp.ClientSession() as session:
            async with session.post(api_url, json=params) as resp:
                if resp.status != 200:
                    self.logger.warning(f"图片URL获取失败({resp.status})")
                    return None
                
                json_data = await resp.json()
                if not json_data.get('success'):
                    self.logger.warning(f"图片API返回错误({json_data.get('message', '未知错误')})")
                    return None
                
                if not json_data.get('data') or len(json_data['data']) == 0:
                    self.logger.warning("图片API返回空数据")
                    return None
                
                image_info = json_data['data'][0]
                if not image_info.get('urlsList'):
                    self.logger.warning("图片URL列表为空")
                    return None
                
                for url_info in image_info['urlsList']:
                    if url_info.get('urlSize') == 'original':
                        self.logger.info(f"获取到图片URL: {url_info['url']}")
                        return url_info['url']
                
                self.logger.warning("未找到original尺寸的图片URL")
                return None
    
    async def _fetch_anosu_image(self) -> Optional[str]:
        api_url = self.api_configs["anosu"].url
        params = {
            "num": 1,
            "r18": 0,
            "proxy": "i.pixiv.re"
        }
        
        async with aiohttp.ClientSession() as session:
            async with session.get(api_url, params=params) as resp:
                if resp.status != 200:
                    self.logger.warning(f"anosu API请求失败({resp.status})")
                    return None
                
                json_data = await resp.json()
                if not isinstance(json_data, list) or len(json_data) == 0:
                    self.logger.warning("anosu API返回数据格式不正确或为空")
                    return None
                    
                return json_data[0]["url"]
        
    async def _download_image(self, url: str) -> Optional[bytes]:
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as response:
                if response.status == 404:
                    raise aiohttp.ClientError("404 Not Found")
                elif response.status != 200:
                    raise aiohttp.ClientError(f"HTTP Error {response.status}")
                
                return await response.read()
            
