import discord
from discord import ui
from discord.ext import commands
from discord import app_commands
import asyncio
import logging
import threading
import time

import database as db

logger = logging.getLogger(__name__)

class VendingBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.members = True
        intents.presences = True
        super().__init__(command_prefix='!', intents=intents)
        self.deposit_callback = None
        self.ready_event = asyncio.Event()
        self.vending_messages = {}
    
    async def setup_hook(self):
        await self.add_cog(VendingCommands(self))
        await self.tree.sync()
        logger.info("Slash commands synced")
    
    async def on_ready(self):
        logger.info(f"Bot logged in as {self.user} (ID: {self.user.id})")
        self.ready_event.set()
        await self.change_presence(activity=discord.Game(name="냥코 KEY 자판기 🐱"))
        await self.restore_vending_machines()
        # 5분 핑 자동 전송 시작 (중복 방지)
        if not getattr(self, '_ping_task_started', False):
            self._ping_task_started = True
            self.loop.create_task(self.ping_loop())
    
    async def ping_loop(self):
        """5분마다 대시보드/자판기 핑 자동 전송 (비블로킹)"""
        while True:
            try:
                # 대시보드 핑 (스레드에서 실행하여 이벤트 루프 블로킹 방지)
                dashboard_url = db.get_setting('dashboard_url', '')
                if dashboard_url:
                    def _do_ping(url=dashboard_url):
                        import requests as _req
                        try:
                            resp = _req.get(url.rstrip('/') + '/ping', timeout=10)
                            return resp.status_code
                        except Exception as e:
                            logger.warning(f"[PING] Dashboard error: {e}")
                            return None
                    
                    status = await self.loop.run_in_executor(None, _do_ping)
                    if status:
                        logger.info(f"[PING] Dashboard: {status}")
                
                # 자판기 새로고침 (재고/상품 상태 최신화)
                await self.refresh_vending_machines()
                logger.info("[PING] Vending machines refreshed")
            except Exception as e:
                logger.error(f"[PING] Error: {e}")
            await asyncio.sleep(300)  # 5분
    
    async def on_message(self, message):
        """메시지 처리 - 리뷰는 임베드 버튼(모달)으로만 작성"""
        if message.author.bot:
            return
        
        # 일반 메시지 처리 (DM 포함 - 일반 채팅은 리뷰로 등록하지 않음)
        await self.process_commands(message)
    
    async def post_review(self, user, order_id, product_name, rating, content):
        """리뷰를 리뷰 채널에 임베드로 게시"""
        try:
            review_channel_id = db.get_setting('review_channel_id', '')
            if not review_channel_id:
                return False, "리뷰 채널이 설정되지 않았습니다. 관리자에게 문의해주세요."
            
            try:
                review_channel = self.get_channel(int(review_channel_id))
                if not review_channel:
                    review_channel = await self.fetch_channel(int(review_channel_id))
            except discord.NotFound:
                return False, f"리뷰 채널(ID: {review_channel_id})을 찾을 수 없습니다. 관리자에게 문의해주세요."
            
            if not review_channel:
                return False, "리뷰 채널을 찾을 수 없습니다. 관리자에게 문의해주세요."
            
            stars = '⭐' * int(rating) + '☆' * (5 - int(rating))
            
            embed = discord.Embed(
                title="⭐ 구매 후기",
                description=content,
                color=discord.Color.gold()
            )
            embed.set_author(name=user.display_name, icon_url=user.display_avatar.url)
            embed.add_field(name="⭐ 별점", value=stars, inline=True)
            embed.add_field(name="📦 상품", value=product_name, inline=True)
            embed.add_field(name="🧾 주문번호", value=f"#{order_id}", inline=True)
            embed.set_footer(text="냥코 KEY 자판기 🐱 | 구매 후기")
            
            await review_channel.send(embed=embed)
            
            logger.info(f"Review registered from {user} for order #{order_id} ({rating} stars)")
            return True, "후기가 등록되었습니다!"
        except Exception as e:
            logger.error(f"Post review error: {e}")
            return False, f"후기 등록 실패: {str(e)}"
    
    async def restore_vending_machines(self):
        channels = db.get_vending_channels()
        for ch in channels:
            try:
                channel = self.get_channel(int(ch['channel_id']))
                if channel:
                    await self.update_vending_machine(channel)
                    logger.info(f"Vending machine restored in channel {ch['channel_id']}")
            except Exception as e:
                logger.error(f"Restore vending machine error: {e}")
    
    async def update_vending_machine(self, channel):
        """Components V2 자판기 - 임베드 컨테이너 안에 드롭다운+버튼 통합"""
        view = VendingMainView()
        
        existing = db.get_vending_message(channel.id)
        if existing:
            try:
                msg = await channel.fetch_message(int(existing['message_id']))
                await msg.edit(view=view)
                return
            except Exception as e:
                logger.warning(f"Vending edit failed, resending: {e}")
        
        msg = await channel.send(view=view)
        db.set_vending_message(channel.id, msg.id)
    
    async def grant_purchase_role(self, member, product_name=None):
        """구매 완료 시 지정된 역할 자동 지급"""
        role_id = db.get_setting('purchase_role_id', '')
        if not role_id:
            return False
        
        try:
            role = member.guild.get_role(int(role_id))
            if not role:
                logger.warning(f"구매 역할을 찾을 수 없습니다: {role_id}")
                return False
            
            if role not in member.roles:
                await member.add_roles(role, reason="냥코 자판기 상품 구매")
                return True
            return False  # 이미 역할 보유
        except Exception as e:
            logger.error(f"구매 역할 지급 오류: {e}")
            return False
    
    async def request_review_dm(self, user_id, order_id, product_name):
        """구매 후 후기 요청 DM 전송 (임베드 + 후기 작성 버튼)"""
        try:
            user = await self.fetch_user(int(user_id))
            
            embed = discord.Embed(
                title="⭐ 구매 완료 & 후기 요청",
                description=f"**{product_name}** 구매가 완료되었습니다! 🎉\n\n"
                           f"만족하셨다면 아래 **[✍️ 후기 작성]** 버튼을 눌러\n"
                           f"별점과 함께 후기를 남겨주세요!",
                color=discord.Color.gold()
            )
            embed.add_field(name="🧾 주문번호", value=f"#{order_id}", inline=True)
            embed.add_field(name="📦 상품", value=product_name, inline=True)
            embed.set_footer(text="냥코 KEY 자판기 🐱 | 후기는 리뷰 채널에 자동 등록됩니다")
            
            view = ReviewRequestView(order_id, product_name)
            await user.send(embed=embed, view=view)
            return True
        except Exception as e:
            logger.error(f"Review request DM error: {e}")
            return False
    
    def run_bot(self, token):
        def _run():
            try:
                self.run(token)
            except Exception as e:
                logger.error(f"Bot run error: {e}")
        thread = threading.Thread(target=_run, daemon=True)
        thread.start()
        return thread
    
    async def send_dm(self, user_id, content=None, embed=None):
        try:
            user = await self.fetch_user(int(user_id))
            if embed and content:
                await user.send(content, embed=embed)
            elif embed:
                await user.send(embed=embed)
            elif content:
                await user.send(content)
            else:
                await user.send("안녕하세요!")
            return True
        except Exception as e:
            logger.error(f"DM send error to {user_id}: {e}")
            return False
    
    async def confirm_order(self, order_id):
        order = db.get_order(order_id)
        if not order:
            return False, "주문을 찾을 수 없습니다."
        if order['status'] != 'pending':
            return False, f"이미 처리된 주문입니다. (상태: {order['status']})"
        
        key = db.get_available_key(order['product_id'])
        if not key:
            db.update_order_status(order_id, 'cancelled')
            await self.send_dm(order['user_id'], 
                f"❌ **주문 #{order_id} 취소 안내**\n\n죄송합니다. 재고가 소진되어 주문이 자동 취소되었습니다.\n주문번호: #{order_id}")
            return False, "재고가 없어 주문을 취소했습니다."
        
        conn = db.get_conn()
        c = conn.cursor()
        c.execute('UPDATE keys SET used=1, order_id=? WHERE id=?', (order_id, key['id']))
        conn.commit()
        conn.close()
        
        db.update_order_status(order_id, 'completed', key_id=key['id'])
        
        embed = discord.Embed(
            title="✅ 구매 완료!",
            description=f"**{order['product_name']}** 구매가 완료되었습니다!",
            color=discord.Color.green()
        )
        embed.add_field(name="주문번호", value=f"#{order_id}", inline=True)
        embed.add_field(name="상품", value=order['product_name'], inline=True)
        embed.add_field(name="결제금액", value=f"{order['amount']:,}원", inline=True)
        embed.add_field(name="🔑 KEY", value=f"```{key['key_value']}```", inline=False)
        embed.set_footer(text="냥코 KEY 자판기 🐱 | 이용해주셔서 감사합니다!")
        
        await self.send_dm(order['user_id'], embed=embed)
        await self.refresh_vending_machines()
        
        return True, f"주문 #{order_id} 완료! 키가 전송되었습니다."
    
    async def cancel_order(self, order_id):
        order = db.get_order(order_id)
        if not order:
            return False, "주문을 찾을 수 없습니다."
        if order['status'] != 'pending':
            return False, f"이미 처리된 주문입니다. (상태: {order['status']})"
        
        db.update_order_status(order_id, 'cancelled')
        await self.send_dm(order['user_id'],
            f"❌ **주문 #{order_id} 취소 안내**\n\n주문이 취소되었습니다.\n주문번호: #{order_id}\n상품: {order['product_name']}")
        return True, f"주문 #{order_id} 취소 완료!"
    
    async def refresh_vending_machines(self):
        channels = db.get_vending_channels()
        for ch in channels:
            try:
                channel = self.get_channel(int(ch['channel_id']))
                if channel:
                    await self.update_vending_machine(channel)
            except Exception as e:
                logger.error(f"Refresh vending machine error: {e}")
    
    async def send_stock_notice(self, product, key_count):
        """재고(키) 입고 시 입고 채널에 공지 전송"""
        stock_channel_id = db.get_setting('stock_channel_id', '')
        if not stock_channel_id:
            return
        
        try:
            channel = self.get_channel(int(stock_channel_id))
            if channel:
                total_stock = db.count_available_keys(product['id'])
                embed = discord.Embed(
                    title="📦 재고 입고 완료!",
                    description=f"**{product['name']}** 재고가 입고되었습니다!",
                    color=discord.Color.green()
                )
                embed.add_field(name="입고 수량", value=f"**+{key_count}개**", inline=True)
                embed.add_field(name="현재 재고", value=f"**{total_stock}개**", inline=True)
                embed.add_field(name="가격", value=f"**{product['price']:,}원**", inline=True)
                embed.set_footer(text="냥코 KEY 자판기 🐱 | 재고 입고 알림")
                await channel.send(embed=embed)
        except Exception as e:
            logger.error(f"Stock notice error: {e}")
    
    async def send_notice(self, title, content, color_hex=None, image_file=None):
        """공지 임베드 전송 (대시보드 및 봇 명령에서 사용) - 이미지 파일 지원"""
        notice_channel_id = db.get_setting('notice_channel_id', '')
        if not notice_channel_id:
            return False, "공지 채널이 설정되지 않았습니다. 대시보드 설정에서 공지 채널 ID를 입력하고 저장해주세요."
        
        try:
            # fetch_channel 사용: 캐시에 없어도 API로 직접 조회
            try:
                channel = self.get_channel(int(notice_channel_id))
                if not channel:
                    channel = await self.fetch_channel(int(notice_channel_id))
            except discord.NotFound:
                return False, f"공지 채널(ID: {notice_channel_id})을 찾을 수 없습니다. 채널 ID가 올바른지 확인해주세요."
            
            if not channel:
                return False, "공지 채널을 찾을 수 없습니다."
            
            try:
                if color_hex and color_hex.startswith('#'):
                    color_hex = color_hex.replace('#', '0x')
                embed_color = discord.Color(int(color_hex or '0x3498db', 16))
            except:
                embed_color = discord.Color.blue()
            
            embed = discord.Embed(
                title=title,
                description=content,
                color=embed_color
            )
            embed.set_footer(text="냥코 KEY 자판기 🐱 | 공지")
            
            # 이미지 파일이 있으면 첨부
            if image_file:
                file = discord.File(image_file, filename="notice_image.png")
                embed.set_image(url="attachment://notice_image.png")
                await channel.send(embed=embed, file=file)
            else:
                await channel.send(embed=embed)
            
            return True, "공지가 전송되었습니다!"
        except Exception as e:
            logger.error(f"Send notice error: {e}")
            return False, f"공지 전송 실패: {str(e)}"


# ============ VENDING MACHINE VIEWS ============

class VendingMainView(ui.LayoutView):
    """Components V2: 임베드 컨테이너 안에 드롭다운+버튼이 통합됨"""
    def __init__(self):
        super().__init__(timeout=None)
        
        title = db.get_setting('vending_title', '냥코대전생 KEY shop')
        desc = db.get_setting('vending_desc', '**원하시는 버튼을 선택해주세요.**')
        footer = db.get_setting('vending_footer', '냥코 KEY 자판기 🐱 | 24시간 운영')
        
        try:
            color_hex = db.get_setting('vending_color', '#5865F2')
            if color_hex.startswith('#'):
                color_hex = color_hex.replace('#', '0x')
            accent = discord.Color(int(color_hex, 16))
        except:
            accent = discord.Color.blurple()
        
        # 실시간 현황 텍스트
        products = db.get_products(active_only=True)
        total_stock = sum(db.count_available_keys(p['id']) for p in products)
        status_text = f"-# 📦 등록 상품 **{len(products)}개** · 🔑 총 재고 **{total_stock}개** · 🟢 재고있음 🔴 품절"
        
        children = [
            ui.TextDisplay(f"## {title}"),
            ui.TextDisplay(desc),
            ui.TextDisplay("### 📂 카테고리 / 상품 선택"),
            ui.ActionRow(CategorySelect()),
            ui.ActionRow(
                VendingButton(label="제품", emoji="🎁", style=discord.ButtonStyle.primary, custom_id="vending_products"),
                VendingButton(label="충전", emoji="💰", style=discord.ButtonStyle.success, custom_id="vending_charge"),
                VendingButton(label="정보", emoji="ℹ️", style=discord.ButtonStyle.secondary, custom_id="vending_info"),
                VendingButton(label="문의", emoji="🎫", style=discord.ButtonStyle.danger, custom_id="vending_ticket"),
            ),
            ui.TextDisplay(status_text),
            ui.TextDisplay(f"-# {footer}"),
        ]
        
        # 배너 이미지 (설정된 경우 컨테이너 하단에 크게 표시)
        banner_url = db.get_setting('vending_banner_url', '')
        if banner_url and banner_url.startswith('http'):
            try:
                gallery = ui.MediaGallery(discord.MediaGalleryItem(media=banner_url))
                children.insert(-2, gallery)
            except Exception as e:
                logger.warning(f"Banner gallery error: {e}")
        
        container = ui.Container(*children, accent_color=accent)
        self.add_item(container)


class CategorySelect(discord.ui.Select):
    """카테고리 선택 드롭다운 - 선택 시 해당 카테고리 상품 구매 메뉴 표시"""
    def __init__(self, row=0):
        categories = db.get_categories()
        products = db.get_products(active_only=True)
        
        options = []
        if categories:
            for cat in categories[:24]:
                cat_count = len(db.get_products_by_category(cat, active_only=True))
                options.append(discord.SelectOption(
                    label=cat,
                    description=f"{cat_count}개 상품",
                    emoji="📂"
                ))
        else:
            for p in products[:24]:
                stock = db.count_available_keys(p['id'])
                options.append(discord.SelectOption(
                    label=f"{p['name']} ({p['price']:,}원)",
                    description=f"재고: {stock}개",
                    value=str(p['id']),
                    emoji="🛒"
                ))
        
        if not options:
            options = [discord.SelectOption(label="상품 준비중", value="none", emoji="⏳")]
        
        placeholder = "사용할 카테고리를 선택하세요" if categories else "구매할 상품을 선택하세요"
        
        super().__init__(
            placeholder=placeholder,
            min_values=1,
            max_values=1,
            options=options,
            custom_id="vending_category_select",
            row=row
        )
    
    async def callback(self, interaction: discord.Interaction):
        selected = self.values[0]
        
        if selected == "none":
            await interaction.response.send_message("⏳ 아직 상품이 등록되지 않았습니다.", ephemeral=True)
            return
        
        # 카테고리 모드인지 상품 ID 모드인지 판단
        categories = db.get_categories()
        
        if categories and selected in categories:
            # 카테고리 선택 → 해당 카테고리 상품 구매 메뉴
            cat_products = db.get_products_by_category(selected, active_only=True)
            if not cat_products:
                await interaction.response.send_message("❌ 해당 카테고리에 상품이 없습니다.", ephemeral=True)
                return
            
            balance = db.get_balance(str(interaction.user.id))
            
            embed = discord.Embed(
                title=f"📂 {selected}",
                description=f"구매할 상품을 선택하세요!\n\n💰 현재 포인트: **{balance:,}원**",
                color=discord.Color.blurple()
            )
            for p in cat_products:
                stock = db.count_available_keys(p['id'])
                icon = '🟢' if stock > 0 else '🔴'
                embed.add_field(
                    name=p['name'],
                    value=f"{icon} **{p['price']:,}원** · 재고 {stock}개\n{p.get('description', '')}",
                    inline=False
                )
            
            await interaction.response.send_message(embed=embed, view=BuyProductView(cat_products), ephemeral=True)
        else:
            # 상품 직접 선택 → 구매 확인
            try:
                pid = int(selected)
            except:
                await interaction.response.send_message("❌ 잘못된 선택입니다.", ephemeral=True)
                return
            
            product = db.get_product(pid)
            if not product or not product['active']:
                await interaction.response.send_message("❌ 판매 중지된 상품입니다.", ephemeral=True)
                return
            
            stock = db.count_available_keys(pid)
            if stock <= 0:
                await interaction.response.send_message("❌ 재고가 소진되었습니다.", ephemeral=True)
                return
            
            balance = db.get_balance(str(interaction.user.id))
            if balance < product['price']:
                await interaction.response.send_message(
                    f"❌ 포인트가 부족합니다!\n필요: {product['price']:,}포인트 | 보유: {balance:,}포인트\n\n💰 **충전** 버튼을 눌러 충전해주세요!",
                    ephemeral=True
                )
                return
            
            await interaction.response.send_message(
                f"✅ **{product['name']}** - {product['price']:,}포인트\n\n"
                f"구매를 진행하시겠습니까?\n"
                f"🎟️ 쿠폰이 있다면 **쿠폰 사용** 버튼을 눌러 할인받으세요! (없으면 바로 구매 확정)",
                view=ConfirmBuyView(product),
                ephemeral=True
            )


class VendingButton(discord.ui.Button):
    async def callback(self, interaction: discord.Interaction):
        action = self.custom_id.split('_')[1] if len(self.custom_id.split('_')) > 1 else ''
        
        if action == 'products':
            await self.show_products(interaction)
        elif action == 'charge':
            await self.show_charge(interaction)
        elif action == 'buy':
            await self.show_buy_menu(interaction)
        elif action == 'info':
            await self.show_info(interaction)
        elif action == 'ticket':
            await self.show_ticket(interaction)
    
    async def show_products(self, interaction):
        # defer 먼저 호출하여 interaction 3초 제한 초과 방지
        await interaction.response.defer(ephemeral=True)
        
        products = db.get_products(active_only=True)
        
        embed = discord.Embed(
            title="📦 제품 목록",
            description="현재 판매 중인 상품입니다.",
            color=discord.Color.blue()
        )
        
        if not products:
            embed.add_field(name="⚠️ 상품 준비중", value="곧 상품이 등록됩니다!", inline=False)
        else:
            # 카테고리 사용 여부 확인
            use_categories = db.get_setting('use_categories', 'false').lower() == 'true'
            if use_categories:
                # 카테고리별로 그룹화하여 표시
                categories = db.get_categories()
                if categories:
                    for cat in categories:
                        cat_products = db.get_products_by_category(cat, active_only=True)
                        if not cat_products:
                            continue
                        lines = []
                        for p in cat_products:
                            stock = db.count_available_keys(p['id'])
                            stock_text = f"{stock}개" if stock > 0 else "품절"
                            lines.append(f"• {p['name']} - **{p['price']:,}원** (재고: {stock_text})")
                        embed.add_field(name=f"📂 {cat}", value="\n".join(lines), inline=False)
                    # 카테고리 없는 상품
                    no_cat = [p for p in products if not p.get('category')]
                    if no_cat:
                        lines = []
                        for p in no_cat:
                            stock = db.count_available_keys(p['id'])
                            stock_text = f"{stock}개" if stock > 0 else "품절"
                            lines.append(f"• {p['name']} - **{p['price']:,}원** (재고: {stock_text})")
                        embed.add_field(name="📦 기타", value="\n".join(lines), inline=False)
                else:
                    # 카테고리가 없으면 전체 표시
                    for p in products:
                        stock = db.count_available_keys(p['id'])
                        embed.add_field(
                            name=f"{p['name']} - {p['price']:,}원",
                            value=f"{p['description']}\n재고: {stock}개",
                            inline=False
                        )
            else:
                for p in products:
                    stock = db.count_available_keys(p['id'])
                    embed.add_field(
                        name=f"{p['name']} - {p['price']:,}원",
                        value=f"{p['description']}\n재고: {stock}개",
                        inline=False
                    )
        
        embed.set_footer(text="🛒 구매 버튼을 눌러 구매할 수 있습니다!")
        await interaction.followup.send(embed=embed, ephemeral=True)
    
    async def show_charge(self, interaction):
        # 계좌 정보는 숨기고 충전 신청 버튼만 표시
        embed = discord.Embed(
            title="💰 포인트 충전",
            description=f"**충전 신청** 버튼을 눌러 충전을 진행해주세요!\n\n"
                       f"**충전 절차**\n"
                       f"1️⃣ 아래 버튼을 눌러 충전 신청\n"
                       f"2️⃣ 입금자명과 충전 금액 입력\n"
                       f"3️⃣ 신청 완료 후 입금 계좌가 표시됩니다\n"
                       f"4️⃣ 입금 확인 후 자동으로 포인트 충전!",
            color=discord.Color.green()
        )
        embed.add_field(name="💰 환율", value="1원 = 1포인트", inline=True)
        embed.set_footer(text="냥코 KEY 자판기 🐱")
        
        view = ChargeRequestView()
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
    
    async def show_buy_menu(self, interaction):
        # defer 먼저 호출하여 interaction 3초 제한 초과 방지
        await interaction.response.defer(ephemeral=True)
        
        products = db.get_products(active_only=True)
        
        if not products:
            await interaction.followup.send("❌ 현재 구매할 수 있는 상품이 없습니다.", ephemeral=True)
            return
        
        # Check balance
        balance = db.get_balance(str(interaction.user.id))
        
        embed = discord.Embed(
            title="🛒 상품 구매",
            description=f"구매할 상품을 선택하세요!\n\n💰 현재 포인트: **{balance:,}원**",
            color=discord.Color.blue()
        )
        
        # 카테고리 사용 여부 확인
        use_categories = db.get_setting('use_categories', 'false').lower() == 'true'
        
        if use_categories:
            categories = db.get_categories()
            if categories:
                for cat in categories:
                    cat_products = db.get_products_by_category(cat, active_only=True)
                    if not cat_products:
                        continue
                    embed.add_field(
                        name=f"📂 {cat}",
                        value="\n".join([f"• {p['name']} - **{p['price']:,}원** (재고: {db.count_available_keys(p['id'])}개)" for p in cat_products]),
                        inline=False
                    )
                # 카테고리 없는 상품
                no_cat = [p for p in products if not p.get('category')]
                if no_cat:
                    embed.add_field(
                        name="📦 기타",
                        value="\n".join([f"• {p['name']} - **{p['price']:,}원** (재고: {db.count_available_keys(p['id'])}개)" for p in no_cat]),
                        inline=False
                    )
            else:
                for p in products:
                    stock = db.count_available_keys(p['id'])
                    embed.add_field(
                        name=f"{p['name']} - {p['price']:,}원",
                        value=f"재고: {stock}개",
                        inline=False
                    )
        else:
            for p in products:
                stock = db.count_available_keys(p['id'])
                embed.add_field(
                    name=f"{p['name']} - {p['price']:,}원",
                    value=f"재고: {stock}개",
                    inline=False
                )
        
        await interaction.followup.send(embed=embed, view=BuyProductView(products), ephemeral=True)
    
    async def show_info(self, interaction):
        # defer 먼저 호출하여 interaction 3초 제한 초과 방지
        await interaction.response.defer(ephemeral=True)
        
        user = db.get_or_create_user(str(interaction.user.id), interaction.user.name)
        balance = user['balance']
        
        # Get recent purchases
        orders = db.get_orders(user_id=str(interaction.user.id), limit=5)
        transactions = db.get_user_transactions(str(interaction.user.id), limit=5)
        
        embed = discord.Embed(
            title="ℹ️ 내 정보",
            description=f"**{interaction.user.display_name}** 님의 자판기 정보입니다.",
            color=discord.Color.blue()
        )
        embed.add_field(name="💰 포인트 잔액", value=f"**{balance:,}포인트**", inline=True)
        embed.add_field(name="📦 구매 횟수", value=f"{sum(1 for o in orders if o['status'] == 'completed')}회", inline=True)
        
        # 거래 타입 한글 매핑
        trans_type_map = {
            'charge': '충전',
            'purchase': '구매',
            'admin_add': '관리자 지급',
            'admin_remove': '관리자 차감',
            'refund': '환불'
        }
        
        if transactions:
            trans_text = []
            for t in transactions[:5]:
                emoji = '🟢' if t['amount'] > 0 else '🔴'
                trans_label = trans_type_map.get(t['type'], t['type'])
                trans_text.append(f"{emoji} {trans_label}: {t['amount']:,}포인트 ({t['created_at']})")
            embed.add_field(name="📋 최근 거래", value="\n".join(trans_text), inline=False)
        else:
            embed.add_field(name="📋 최근 거래", value="거래 내역이 없습니다.", inline=False)
        
        embed.set_footer(text="냥코 KEY 자판기 🐱")
        await interaction.followup.send(embed=embed, ephemeral=True)
    
    async def show_ticket(self, interaction):
        """문의 티켓 생성"""
        # Check if user already has an open ticket
        existing = db.get_open_ticket_by_user(str(interaction.user.id))
        if existing:
            try:
                channel = interaction.client.get_channel(int(existing['channel_id']))
                if channel:
                    await interaction.response.send_message(
                        f"❌ **이미 열려있는 문의 티켓이 있습니다!**\n\n"
                        f"📌 티켓 채널: {channel.mention}\n"
                        f"티켓을 닫은 후 새로 생성할 수 있습니다.",
                        ephemeral=True
                    )
                    return
            except:
                pass
        
        # Get ticket category
        category_id = db.get_setting('ticket_category_id', '')
        if not category_id:
            await interaction.response.send_message(
                "❌ 문의 시스템이 아직 설정되지 않았습니다.\n관리자에게 문의해주세요.",
                ephemeral=True
            )
            return
        
        try:
            category = interaction.client.get_channel(int(category_id))
            if not category or not isinstance(category, discord.CategoryChannel):
                await interaction.response.send_message(
                    "❌ 문의 카테고리가 올바르지 않습니다.\n관리자에게 문의해주세요.",
                    ephemeral=True
                )
                return
            
            # Create ticket channel
            overwrites = {
                interaction.guild.default_role: discord.PermissionOverwrite(view_channel=False),
                interaction.user: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_messages=True, read_message_history=True),
                interaction.guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_messages=True, read_message_history=True)
            }
            
            # Add admin permissions
            admins = db.get_admins()
            for admin in admins:
                admin_member = interaction.guild.get_member(int(admin['user_id']))
                if admin_member:
                    overwrites[admin_member] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_messages=True, read_message_history=True)
            
            channel_name = f"문의-{interaction.user.name}"
            ticket_channel = await category.create_text_channel(
                name=channel_name,
                overwrites=overwrites
            )
            
            # Create ticket in DB
            tid = db.create_ticket(str(interaction.user.id), interaction.user.name, ticket_channel.id)
            if tid == -1:
                await ticket_channel.delete()
                await interaction.response.send_message(
                    "❌ 이미 열려있는 문의 티켓이 있습니다!",
                    ephemeral=True
                )
                return
            
            # Send ticket message
            embed = discord.Embed(
                title=f"🎫 문의 티켓 #{tid}",
                description=f"**{interaction.user.mention}** 님의 문의 티켓입니다.\n\n"
                           f"문의 내용을 이 채널에 작성해주세요!\n"
                           f"관리자가 확인 후 답변드리겠습니다.",
                color=discord.Color.blue()
            )
            embed.add_field(name="📌 안내", value="티켓이 해결되면 아래 버튼을 눌러 티켓을 닫을 수 있습니다.", inline=False)
            embed.set_footer(text="냥코 KEY 자판기 🐱 | 문의 시스템")
            
            view = TicketView(tid)
            await ticket_channel.send(embed=embed, view=view)
            
            await interaction.response.send_message(
                f"✅ 문의 티켓이 생성되었습니다!\n📌 채널: {ticket_channel.mention}",
                ephemeral=True
            )
            
        except Exception as e:
            logger.error(f"Ticket creation error: {e}")
            await interaction.response.send_message(
                f"❌ 티켓 생성 중 오류가 발생했습니다: {str(e)}",
                ephemeral=True
            )


class TicketView(discord.ui.View):
    """Ticket management view"""
    def __init__(self, ticket_id):
        super().__init__(timeout=None)
        self.ticket_id = ticket_id
        self.add_item(CloseTicketButton(ticket_id, row=0))


class CloseTicketButton(discord.ui.Button):
    def __init__(self, ticket_id, row=0):
        super().__init__(
            label="🔒 티켓 닫기",
            style=discord.ButtonStyle.danger,
            custom_id=f"ticket_close_{ticket_id}",
            row=row
        )
        self.ticket_id = ticket_id
    
    async def callback(self, interaction: discord.Interaction):
        ticket = db.get_ticket(self.ticket_id)
        if not ticket:
            await interaction.response.send_message("❌ 티켓을 찾을 수 없습니다.", ephemeral=True)
            return
        
        # Check if user is ticket owner or admin
        is_owner = str(interaction.user.id) == ticket['user_id']
        is_admin = db.is_admin(str(interaction.user.id))
        
        if not is_owner and not is_admin:
            await interaction.response.send_message("❌ 이 티켓을 닫을 권한이 없습니다.", ephemeral=True)
            return
        
        # Close ticket
        db.close_ticket(self.ticket_id)
        
        embed = discord.Embed(
            title="🔒 티켓 닫힘",
            description=f"티켓 #{self.ticket_id}이(가) 닫혔습니다.\n\n이 채널은 5초 후 삭제됩니다.",
            color=discord.Color.red()
        )
        await interaction.response.edit_message(embed=embed, view=None)
        
        await asyncio.sleep(5)
        try:
            await interaction.channel.delete()
        except:
            pass


class ReviewRequestView(discord.ui.View):
    """후기 작성 버튼 뷰 (DM용)"""
    def __init__(self, order_id, product_name):
        super().__init__(timeout=None)
        self.order_id = order_id
        self.product_name = product_name
        self.add_item(ReviewButton(order_id, product_name))


class ReviewButton(discord.ui.Button):
    def __init__(self, order_id, product_name):
        super().__init__(
            label="✍️ 후기 작성",
            style=discord.ButtonStyle.success,
            custom_id=f"review_write_{order_id}",
            emoji=None
        )
        self.order_id = order_id
        self.product_name = product_name
    
    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_modal(ReviewModal(self.order_id, self.product_name))


class ReviewModal(discord.ui.Modal):
    """후기 작성 모달 (별점 + 내용)"""
    def __init__(self, order_id, product_name):
        super().__init__(title=f"⭐ 후기 작성 - {product_name[:20]}")
        self.order_id = order_id
        self.product_name = product_name
        
        self.rating = discord.ui.TextInput(
            label="별점 (1~5)",
            placeholder="1부터 5까지 숫자로 입력하세요 (예: 5)",
            required=True,
            max_length=1
        )
        self.add_item(self.rating)
        
        self.content = discord.ui.TextInput(
            label="후기 내용",
            style=discord.TextStyle.paragraph,
            placeholder="상품에 대한 솔직한 후기를 남겨주세요!",
            required=True,
            max_length=1000
        )
        self.add_item(self.content)
    
    async def on_submit(self, interaction: discord.Interaction):
        try:
            rating = int(self.rating.value.strip())
        except:
            await interaction.response.send_message("❌ 별점은 1~5 사이의 숫자로 입력해주세요.", ephemeral=True)
            return
        
        if rating < 1 or rating > 5:
            await interaction.response.send_message("❌ 별점은 1~5 사이의 숫자로 입력해주세요.", ephemeral=True)
            return
        
        content = self.content.value.strip()
        if len(content) < 2:
            await interaction.response.send_message("❌ 후기가 너무 짧습니다! 조금 더 길게 작성해주세요.", ephemeral=True)
            return
        
        # 리뷰 채널에 게시
        success, msg = await interaction.client.post_review(
            interaction.user, self.order_id, self.product_name, rating, content
        )
        
        if success:
            confirm_embed = discord.Embed(
                title="✅ 후기가 등록되었습니다!",
                description="소중한 후기 감사합니다! 😊\n\n후기는 리뷰 채널에 등록되었습니다.",
                color=discord.Color.green()
            )
            confirm_embed.set_footer(text="냥코 KEY 자판기 🐱")
            await interaction.response.send_message(embed=confirm_embed, ephemeral=True)
        else:
            await interaction.response.send_message(f"❌ {msg}", ephemeral=True)


class ChargeRequestView(discord.ui.View):
    """Charge request button view"""
    def __init__(self):
        super().__init__(timeout=120)
        self.add_item(ChargeRequestButton(label="💳 충전 신청", style=discord.ButtonStyle.success, custom_id="charge_request"))


class ChargeRequestButton(discord.ui.Button):
    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_modal(ChargeRequestModal())


class ChargeRequestModal(discord.ui.Modal):
    """Modal for charge request with depositor name and amount"""
    def __init__(self):
        super().__init__(title="💰 포인트 충전 신청")
        
        self.depositor_name = discord.ui.TextInput(
            label="입금자명",
            placeholder="입금하실 때 사용한 이름을 입력하세요",
            required=True,
            max_length=50
        )
        self.add_item(self.depositor_name)
        
        self.amount = discord.ui.TextInput(
            label="충전 금액 (원)",
            placeholder="입금하실 금액을 숫자로 입력하세요 (예: 10000)",
            required=True,
            max_length=10
        )
        self.add_item(self.amount)
    
    async def on_submit(self, interaction: discord.Interaction):
        depositor_name = self.depositor_name.value.strip()
        try:
            amount = int(self.amount.value.strip().replace(',', ''))
        except:
            await interaction.response.send_message("❌ 금액은 숫자로 입력해주세요.", ephemeral=True)
            return
        
        if amount <= 0:
            await interaction.response.send_message("❌ 0보다 큰 금액을 입력해주세요.", ephemeral=True)
            return
        
        # Get bank info
        bank_account = db.get_setting('bank_account', '미설정')
        bank_holder = db.get_setting('bank_holder', '미설정')
        bank_name = db.get_setting('bank_name', '')
        
        # Create charge request
        rid = db.create_charge_request(str(interaction.user.id), interaction.user.name, depositor_name, amount)
        
        embed = discord.Embed(
            title="✅ 충전 신청 완료!",
            description=f"충전 요청이 접수되었습니다.",
            color=discord.Color.green()
        )
        embed.add_field(name="요청번호", value=f"#{rid}", inline=True)
        embed.add_field(name="입금자명", value=depositor_name, inline=True)
        embed.add_field(name="충전 금액", value=f"{amount:,}원 → {amount:,}포인트", inline=True)
        embed.add_field(name="🏦 입금 계좌", value=f"{bank_name} {bank_account} ({bank_holder})", inline=False)
        embed.add_field(name="📌 안내", value="해당 계좌로 입금하시면 자동으로 확인되어 포인트가 충전됩니다!\n\n⏰ **충전 요청은 5분 뒤 만료됩니다.**\n5분 내에 입금되지 않으면 다시 충전 신청해주세요!", inline=False)
        embed.set_footer(text="냥코 KEY 자판기 🐱")
        
        await interaction.response.send_message(embed=embed, ephemeral=True)


class BuyProductView(discord.ui.View):
    """Product selection view for purchasing"""
    def __init__(self, products):
        super().__init__(timeout=120)
        self.products = products
        # Discord UI row는 0~4까지만 허용 (각 row당 최대 5개 버튼)
        # 상품이 25개를 초과하면 추가 버튼을 이전 row에 몰아서 배치
        for i, p in enumerate(products):
            # 5개씩 row 0~4에 분배 (최대 25개 표시)
            row = min(i // 5, 4)
            self.add_item(BuyButton(p, row=row))
            # 상품이 25개 이상이면 나머지는 더 이상 추가하지 않음
            if i >= 24:
                break


class BuyButton(discord.ui.Button):
    def __init__(self, product, row=0):
        stock = db.count_available_keys(product['id'])
        disabled = stock <= 0 or not product['active']
        label = f"{product['name']} - {product['price']:,}원"
        if stock <= 0:
            label = f"{product['name']} (품절)"
        super().__init__(
            label=label,
            style=discord.ButtonStyle.primary if stock > 0 else discord.ButtonStyle.secondary,
            custom_id=f"buy_confirm_{product['id']}",
            disabled=disabled,
            row=row
        )
        self.product = product
    
    async def callback(self, interaction: discord.Interaction):
        # Check balance
        balance = db.get_balance(str(interaction.user.id))
        if balance < self.product['price']:
            await interaction.response.send_message(
                f"❌ 포인트가 부족합니다!\n"
                f"필요: {self.product['price']:,}포인트 | 보유: {balance:,}포인트\n\n"
                f"💰 **충전** 버튼을 눌러 포인트를 충전해주세요!",
                ephemeral=True
            )
            return
        
        # Check stock
        stock = db.count_available_keys(self.product['id'])
        if stock <= 0:
            await interaction.response.send_message("❌ 재고가 소진되었습니다.", ephemeral=True)
            return
        
        # Confirm purchase
        await interaction.response.send_message(
            f"✅ **{self.product['name']}** - {self.product['price']:,}포인트\n\n"
            f"구매를 진행하시겠습니까?\n"
            f"🎟️ 쿠폰이 있다면 **쿠폰 사용** 버튼을 눌러 할인받으세요! (없으면 바로 구매 확정)",
            view=ConfirmBuyView(self.product),
            ephemeral=True
        )


class ConfirmBuyView(discord.ui.View):
    """Confirm purchase view (쿠폰 할인 지원)"""
    def __init__(self, product, coupon=None):
        super().__init__(timeout=60)
        self.product = product
        self.coupon = coupon
        self.add_item(ConfirmBuyButton(product, confirm=True, row=0, coupon=coupon))
        self.add_item(CouponApplyButton(product, row=0))
        self.add_item(ConfirmBuyButton(product, confirm=False, row=0))


class ConfirmBuyButton(discord.ui.Button):
    def __init__(self, product, confirm=True, row=0, coupon=None):
        if confirm:
            super().__init__(
                label="✅ 구매 확정",
                style=discord.ButtonStyle.success,
                custom_id=f"confirm_yes_{product['id']}",
                row=row
            )
        else:
            super().__init__(
                label="❌ 취소",
                style=discord.ButtonStyle.secondary,
                custom_id=f"confirm_no_{product['id']}",
                row=row
            )
        self.product = product
        self.is_confirm = confirm
        self.coupon = coupon
    
    def get_final_price(self):
        """쿠폰 할인이 적용된 최종 가격"""
        price = self.product['price']
        if self.coupon:
            price = max(0, price - self.coupon['discount_amount'])
        return price
    
    async def callback(self, interaction: discord.Interaction):
        if not self.is_confirm:
            await interaction.response.edit_message(content="❌ 구매가 취소되었습니다.", embed=None, view=None)
            return
        
        # Double check balance and stock (쿠폰 할인 적용 가격 기준)
        final_price = self.get_final_price()
        balance = db.get_balance(str(interaction.user.id))
        if balance < final_price:
            await interaction.response.edit_message(
                content=f"❌ 포인트가 부족합니다! (부족액: {final_price - balance:,}포인트)",
                embed=None, view=None
            )
            return
        
        stock = db.count_available_keys(self.product['id'])
        if stock <= 0:
            await interaction.response.edit_message(content="❌ 재고가 소진되었습니다.", embed=None, view=None)
            return
        
        # Deduct balance (쿠폰 할인 적용)
        coupon_desc = f" (쿠폰 {self.coupon['code']} -{self.coupon['discount_amount']:,})" if self.coupon else ""
        success = db.deduct_balance(
            str(interaction.user.id),
            final_price,
            trans_type='purchase',
            description=f"{self.product['name']} 구매{coupon_desc}"
        )
        
        if not success:
            await interaction.response.edit_message(content="❌ 결제에 실패했습니다.", embed=None, view=None)
            return
        
        # Create order and complete immediately (points purchase = instant)
        order_id = db.create_order(
            str(interaction.user.id),
            interaction.user.name,
            self.product['id'],
            self.product['name'],
            final_price
        )
        
        # Get key and complete
        key = db.get_available_key(self.product['id'])
        if not key:
            # Refund
            db.add_balance(str(interaction.user.id), interaction.user.name, final_price, 'refund', '재고 소진 환불')
            db.update_order_status(order_id, 'cancelled')
            await interaction.response.edit_message(content="❌ 재고가 소진되어 환불되었습니다.", embed=None, view=None)
            return
        
        conn = db.get_conn()
        c = conn.cursor()
        c.execute('UPDATE keys SET used=1, order_id=? WHERE id=?', (order_id, key['id']))
        conn.commit()
        conn.close()
        
        db.update_order_status(order_id, 'completed', key_id=key['id'])
        
        # 쿠폰 사용 처리 (사용 횟수 +1)
        if self.coupon:
            db.use_coupon(self.coupon['id'])
        
        # Send key via DM (customizable message)
        dm_title = db.get_setting('purchase_dm_title', '✅ 구매 완료!')
        dm_desc = db.get_setting('purchase_dm_desc', '**{product}** 구매가 완료되었습니다!')
        dm_footer = db.get_setting('purchase_dm_footer', '냥코 KEY 자판기 🐱 | 이용해주셔서 감사합니다!')
        
        embed = discord.Embed(
            title=dm_title,
            description=dm_desc.replace('{product}', self.product['name']),
            color=discord.Color.green()
        )
        embed.add_field(name="주문번호", value=f"#{order_id}", inline=True)
        embed.add_field(name="상품", value=self.product['name'], inline=True)
        if self.coupon:
            embed.add_field(name="원래 가격", value=f"{self.product['price']:,}포인트", inline=True)
            embed.add_field(name=f"🎟️ 할인 ({self.coupon['code']})", value=f"-{self.coupon['discount_amount']:,}포인트", inline=True)
        embed.add_field(name="결제금액", value=f"{final_price:,}포인트", inline=True)
        embed.add_field(name="🔑 KEY", value=f"```{key['key_value']}```", inline=False)
        embed.set_footer(text=dm_footer)
        
        await interaction.client.send_dm(interaction.user.id, embed=embed)
        
        new_balance = db.get_balance(str(interaction.user.id))
        
        discount_text = f"\n🎟️ 쿠폰 할인: -{self.coupon['discount_amount']:,}포인트 ({self.coupon['code']})" if self.coupon else ""
        await interaction.response.edit_message(
            content=f"✅ **구매 완료!**\n\n"
                   f"📦 상품: {self.product['name']}\n"
                   f"💰 사용 포인트: {final_price:,}포인트{discount_text}\n"
                   f"💳 남은 포인트: {new_balance:,}포인트\n"
                   f"🔑 KEY가 DM으로 전송되었습니다!",
            embed=None, view=None
        )
        
        # Send purchase log to purchase log channel
        from datetime import datetime
        purchase_log_channel_id = db.get_setting('purchase_log_channel_id', '')
        if purchase_log_channel_id and interaction.client.is_ready():
            try:
                channel = interaction.client.get_channel(int(purchase_log_channel_id))
                if channel:
                    embed_log = discord.Embed(
                        title="🛒 구매 완료!",
                        description=f"**{interaction.user.name}** 님이 상품을 구매했습니다!",
                        color=discord.Color.blue(),
                        timestamp=datetime.now()
                    )
                    embed_log.add_field(name="주문번호", value=f"#{order_id}", inline=True)
                    embed_log.add_field(name="구매자", value=f"{interaction.user.mention} ({interaction.user.name})", inline=True)
                    embed_log.add_field(name="상품", value=self.product['name'], inline=True)
                    if self.coupon:
                        embed_log.add_field(name="쿠폰 할인", value=f"{self.coupon['code']} (-{self.coupon['discount_amount']:,})", inline=True)
                    embed_log.add_field(name="결제 금액", value=f"{final_price:,}포인트", inline=True)
                    embed_log.add_field(name="구매 시간", value=datetime.now().strftime('%Y-%m-%d %H:%M:%S'), inline=True)
                    embed_log.set_footer(text="🔑 키는 관리자 패널에서 확인하세요")
                    await channel.send(embed=embed_log)
            except Exception as e:
                logger.error(f"Purchase log channel error: {e}")
        
        # 구매 완료 후 역할 자동 지급
        try:
            if interaction.guild:
                await interaction.client.grant_purchase_role(interaction.user, self.product['name'])
        except Exception as e:
            logger.error(f"Purchase role grant error: {e}")
        
        # 후기 요청 DM 전송
        try:
            await interaction.client.request_review_dm(interaction.user.id, order_id, self.product['name'])
        except Exception as e:
            logger.error(f"Review request DM error: {e}")
        
        # Refresh vending machine stock
        await interaction.client.refresh_vending_machines()


class CouponApplyButton(discord.ui.Button):
    """구매 확인창에서 쿠폰 코드 입력 버튼"""
    def __init__(self, product, row=0):
        super().__init__(
            label="쿠폰 사용",
            emoji="🎟️",
            style=discord.ButtonStyle.primary,
            custom_id=f"coupon_apply_{product['id']}",
            row=row
        )
        self.product = product
    
    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_modal(CouponModal(self.product))


class CouponModal(discord.ui.Modal):
    """쿠폰 코드 입력 모달 - 유효한 쿠폰이면 할인 적용된 구매창으로 갱신"""
    def __init__(self, product):
        super().__init__(title="🎟️ 할인 쿠폰 입력")
        self.product = product
        
        self.code = discord.ui.TextInput(
            label="쿠폰 코드",
            placeholder="쿠폰 코드를 입력하세요 (예: WELCOME1000)",
            required=True,
            max_length=50
        )
        self.add_item(self.code)
    
    async def on_submit(self, interaction: discord.Interaction):
        code = self.code.value.strip()
        coupon = db.get_coupon_by_code(code)
        
        if not coupon:
            await interaction.response.send_message(
                "❌ 유효하지 않은 쿠폰 코드입니다.\n"
                "(존재하지 않거나, 비활성화되었거나, 사용 횟수가 모두 소진된 쿠폰입니다.)",
                ephemeral=True
            )
            return
        
        discount = coupon['discount_amount']
        final_price = max(0, self.product['price'] - discount)
        
        content = (
            f"✅ **{self.product['name']}**\n\n"
            f"💰 원래 가격: ~~{self.product['price']:,}포인트~~\n"
            f"🎟️ 쿠폰 할인: **-{discount:,}포인트** ({coupon['code']})\n"
            f"💳 결제 금액: **{final_price:,}포인트**\n\n"
            f"구매를 진행하시겠습니까?"
        )
        
        await interaction.response.edit_message(content=content, view=ConfirmBuyView(self.product, coupon))


class VendingCommands(commands.Cog):
    def __init__(self, bot: VendingBot):
        self.bot = bot
    
    def is_admin_check(self, interaction: discord.Interaction) -> bool:
        if not db.get_admins():
            db.add_admin(str(interaction.user.id), interaction.user.name)
            return True
        return db.is_admin(str(interaction.user.id))
    
    # ============ ADMIN COMMANDS ============
    
    @app_commands.command(name="자판기설정", description="[관리자] 현재 채널에 버튼 자판기를 설치합니다")
    async def setup_vending(self, interaction: discord.Interaction):
        if not self.is_admin_check(interaction):
            await interaction.response.send_message("❌ 관리자만 사용할 수 있습니다.", ephemeral=True)
            return
        
        await interaction.response.defer(ephemeral=True)
        try:
            await self.bot.update_vending_machine(interaction.channel)
            await interaction.followup.send("✅ 버튼 자판기가 설치되었습니다!", ephemeral=True)
        except discord.Forbidden:
            await interaction.followup.send(
                "❌ 봇에게 메시지 보내기 권한이 없습니다!\n\n"
                "**해결 방법 (봇 재초대):**\n"
                "아래 링크로 봇을 다시 초대하면 모든 권한이 포함됩니다.\n\n"
                f"https://discord.com/oauth2/authorize?client_id={self.bot.user.id}&permissions=8&scope=bot+applications.commands",
                ephemeral=True
            )
        except Exception as e:
            logger.error(f"Setup vending error: {e}")
            await interaction.followup.send(
                f"❌ 자판기 설치 중 오류가 발생했습니다: {str(e)}",
                ephemeral=True
            )
    
    @app_commands.command(name="자판기새로고침", description="[관리자] 자판기 메시지를 새로고침합니다")
    async def refresh_vending(self, interaction: discord.Interaction):
        if not self.is_admin_check(interaction):
            await interaction.response.send_message("❌ 관리자만 사용할 수 있습니다.", ephemeral=True)
            return
        
        await interaction.response.defer(ephemeral=True)
        await self.bot.refresh_vending_machines()
        await interaction.followup.send("✅ 자판기가 새로고침되었습니다!", ephemeral=True)
    
    @app_commands.command(name="포인트지급", description="[관리자] 유저에게 포인트를 지급합니다")
    @app_commands.describe(user="포인트를 지급할 유저", amount="포인트 금액")
    async def give_points(self, interaction: discord.Interaction, user: discord.User, amount: int):
        if not self.is_admin_check(interaction):
            await interaction.response.send_message("❌ 관리자만 사용할 수 있습니다.", ephemeral=True)
            return
        
        if amount <= 0:
            await interaction.response.send_message("❌ 0보다 큰 금액을 입력해주세요.", ephemeral=True)
            return
        
        new_balance = db.add_balance(
            str(user.id), user.name, amount,
            trans_type='admin_add',
            description=f"관리자 {interaction.user.name}님이 {amount:,}포인트 지급"
        )
        
        # Notify user via DM
        embed = discord.Embed(
            title="💰 포인트 충전 완료!",
            description=f"{amount:,}포인트가 충전되었습니다!",
            color=discord.Color.green()
        )
        embed.add_field(name="충전 포인트", value=f"{amount:,}포인트", inline=True)
        embed.add_field(name="현재 잔액", value=f"{new_balance:,}포인트", inline=True)
        embed.set_footer(text="냥코 KEY 자판기 🐱")
        await self.bot.send_dm(user.id, embed=embed)
        
        await interaction.response.send_message(
            f"✅ {user.mention} 님에게 {amount:,}포인트가 지급되었습니다!\n"
            f"현재 잔액: {new_balance:,}포인트",
            ephemeral=True
        )
    
    @app_commands.command(name="포인트차감", description="[관리자] 유저의 포인트를 차감합니다")
    @app_commands.describe(user="포인트를 차감할 유저", amount="차감할 포인트 금액")
    async def remove_points(self, interaction: discord.Interaction, user: discord.User, amount: int):
        if not self.is_admin_check(interaction):
            await interaction.response.send_message("❌ 관리자만 사용할 수 있습니다.", ephemeral=True)
            return
        
        if amount <= 0:
            await interaction.response.send_message("❌ 0보다 큰 금액을 입력해주세요.", ephemeral=True)
            return
        
        success = db.deduct_balance(
            str(user.id), amount,
            trans_type='admin_remove',
            description=f"관리자 {interaction.user.name}님이 {amount:,}포인트 차감"
        )
        
        if not success:
            await interaction.response.send_message("❌ 유저의 포인트가 부족합니다.", ephemeral=True)
            return
        
        new_balance = db.get_balance(str(user.id))
        
        await interaction.response.send_message(
            f"✅ {user.mention} 님의 {amount:,}포인트가 차감되었습니다!\n"
            f"현재 잔액: {new_balance:,}포인트",
            ephemeral=True
        )
    
    @app_commands.command(name="잔액확인", description="[관리자] 유저의 포인트 잔액을 확인합니다")
    @app_commands.describe(user="잔액을 확인할 유저")
    async def check_balance(self, interaction: discord.Interaction, user: discord.User):
        if not self.is_admin_check(interaction):
            await interaction.response.send_message("❌ 관리자만 사용할 수 있습니다.", ephemeral=True)
            return
        
        balance = db.get_balance(str(user.id))
        user_data = db.get_user(str(user.id))
        
        embed = discord.Embed(
            title="💰 포인트 잔액 확인",
            color=discord.Color.blue()
        )
        embed.add_field(name="유저", value=user.mention, inline=True)
        embed.add_field(name="잔액", value=f"**{balance:,}포인트**", inline=True)
        if user_data:
            embed.add_field(name="가입일", value=user_data['created_at'], inline=True)
        
        await interaction.response.send_message(embed=embed, ephemeral=True)
    
    @app_commands.command(name="수동확인", description="[관리자] 주문을 수동으로 확인합니다")
    @app_commands.describe(order_id="확인할 주문번호")
    async def manual_confirm(self, interaction: discord.Interaction, order_id: int):
        if not self.is_admin_check(interaction):
            await interaction.response.send_message("❌ 관리자만 사용할 수 있습니다.", ephemeral=True)
            return
        
        await interaction.response.defer(ephemeral=True)
        success, msg = await self.bot.confirm_order(order_id)
        await interaction.followup.send(msg, ephemeral=True)
    
    @app_commands.command(name="주문취소", description="[관리자] 주문을 취소합니다")
    @app_commands.describe(order_id="취소할 주문번호")
    async def cancel_order(self, interaction: discord.Interaction, order_id: int):
        if not self.is_admin_check(interaction):
            await interaction.response.send_message("❌ 관리자만 사용할 수 있습니다.", ephemeral=True)
            return
        
        await interaction.response.defer(ephemeral=True)
        success, msg = await self.bot.cancel_order(order_id)
        await interaction.followup.send(msg, ephemeral=True)
    
    @app_commands.command(name="대기주문", description="[관리자] 대기 중인 주문 목록을 확인합니다")
    async def pending_orders(self, interaction: discord.Interaction):
        if not self.is_admin_check(interaction):
            await interaction.response.send_message("❌ 관리자만 사용할 수 있습니다.", ephemeral=True)
            return
        
        pending = db.get_pending_orders()
        if not pending:
            await interaction.response.send_message("✅ 대기 중인 주문이 없습니다.", ephemeral=True)
            return
        
        embed = discord.Embed(
            title=f"⏳ 대기 중인 주문 ({len(pending)}건)",
            color=discord.Color.orange()
        )
        for o in pending[:10]:
            embed.add_field(
                name=f"#{o['id']} - {o['username']}",
                value=f"{o['product_name']} | {o['amount']:,}원 | {o['created_at']}",
                inline=False
            )
        await interaction.response.send_message(embed=embed, ephemeral=True)
    
    @app_commands.command(name="상품추가", description="[관리자] 상품을 추가합니다")
    @app_commands.describe(name="상품명", price="가격(포인트)", description="설명", category="카테고리")
    async def add_product(self, interaction: discord.Interaction, name: str, price: int, description: str = "", category: str = ""):
        if not self.is_admin_check(interaction):
            await interaction.response.send_message("❌ 관리자만 사용할 수 있습니다.", ephemeral=True)
            return
        
        db.add_product(name, price, description, category=category)
        cat_text = f"\n📂 카테고리: {category}" if category else ""
        await interaction.response.send_message(
            f"✅ 상품이 추가되었습니다!\n**{name}** - {price:,}포인트{cat_text}", ephemeral=True)
        await self.bot.refresh_vending_machines()
    
    @app_commands.command(name="상품삭제", description="[관리자] 상품을 삭제합니다")
    @app_commands.describe(product_id="삭제할 상품 ID")
    async def delete_product(self, interaction: discord.Interaction, product_id: int):
        if not self.is_admin_check(interaction):
            await interaction.response.send_message("❌ 관리자만 사용할 수 있습니다.", ephemeral=True)
            return
        
        product = db.get_product(product_id)
        if not product:
            await interaction.response.send_message("❌ 상품을 찾을 수 없습니다.", ephemeral=True)
            return
        
        db.delete_product(product_id)
        await interaction.response.send_message(
            f"✅ 상품이 삭제되었습니다!\n**{product['name']}** - {product['price']:,}포인트",
            ephemeral=True
        )
        await self.bot.refresh_vending_machines()
    
    @app_commands.command(name="키추가", description="[관리자] 상품에 키를 추가합니다")
    @app_commands.describe(product_id="상품 ID", keys="키 목록 (줄바꿈으로 구분)")
    async def add_keys(self, interaction: discord.Interaction, product_id: int, keys: str):
        if not self.is_admin_check(interaction):
            await interaction.response.send_message("❌ 관리자만 사용할 수 있습니다.", ephemeral=True)
            return
        
        product = db.get_product(product_id)
        if not product:
            await interaction.response.send_message("❌ 상품을 찾을 수 없습니다.", ephemeral=True)
            return
        
        key_list = [k.strip() for k in keys.split('\n') if k.strip()]
        db.add_keys(product_id, key_list)
        await interaction.response.send_message(
            f"✅ **{product['name']}**에 키 {len(key_list)}개가 추가되었습니다!", ephemeral=True)
        await self.bot.refresh_vending_machines()
        
        # 입고 채널에 공지 전송
        await self.bot.send_stock_notice(product, len(key_list))
    
    @app_commands.command(name="카테고리토글", description="[관리자] 카테고리 사용 여부를 전환합니다")
    @app_commands.describe(enabled="사용 여부 (true/false)")
    async def toggle_categories(self, interaction: discord.Interaction, enabled: bool):
        if not self.is_admin_check(interaction):
            await interaction.response.send_message("❌ 관리자만 사용할 수 있습니다.", ephemeral=True)
            return
        
        db.set_setting('use_categories', str(enabled))
        status = "활성화" if enabled else "비활성화"
        await interaction.response.send_message(
            f"✅ 카테고리 기능이 **{status}**되었습니다!", ephemeral=True)
        await self.bot.refresh_vending_machines()
    
    # ============ COUPON COMMANDS (할인 쿠폰) ============
    
    @app_commands.command(name="쿠폰생성", description="[관리자] 할인 쿠폰을 생성합니다")
    @app_commands.describe(code="쿠폰 코드 (예: WELCOME1000)", discount_amount="할인 금액(포인트)", max_uses="최대 사용 횟수 (0=무제한)")
    async def create_coupon_cmd(self, interaction: discord.Interaction, code: str, discount_amount: int, max_uses: int = 0):
        if not self.is_admin_check(interaction):
            await interaction.response.send_message("❌ 관리자만 사용할 수 있습니다.", ephemeral=True)
            return
        
        if discount_amount <= 0:
            await interaction.response.send_message("❌ 할인 금액은 0보다 커야 합니다.", ephemeral=True)
            return
        if max_uses < 0:
            await interaction.response.send_message("❌ 최대 사용 횟수는 0 이상이어야 합니다.", ephemeral=True)
            return
        
        cid = db.create_coupon(code, discount_amount, max_uses)
        if cid == -1:
            await interaction.response.send_message(
                f"❌ 이미 존재하는 쿠폰 코드입니다: **{code.strip().upper()}**",
                ephemeral=True
            )
            return
        
        uses_text = f"{max_uses}회" if max_uses > 0 else "무제한"
        await interaction.response.send_message(
            f"✅ 쿠폰이 생성되었습니다!\n\n"
            f"🎟️ 코드: **{code.strip().upper()}**\n"
            f"💰 할인: **{discount_amount:,}포인트**\n"
            f"🔢 사용 가능: **{uses_text}**",
            ephemeral=True
        )
    
    @app_commands.command(name="쿠폰목록", description="[관리자] 등록된 쿠폰 목록을 확인합니다")
    async def coupon_list_cmd(self, interaction: discord.Interaction):
        if not self.is_admin_check(interaction):
            await interaction.response.send_message("❌ 관리자만 사용할 수 있습니다.", ephemeral=True)
            return
        
        coupons = db.get_coupons()
        if not coupons:
            await interaction.response.send_message("✅ 등록된 쿠폰이 없습니다.", ephemeral=True)
            return
        
        embed = discord.Embed(
            title=f"🎟️ 할인 쿠폰 목록 ({len(coupons)}개)",
            color=discord.Color.gold()
        )
        for cp in coupons[:15]:
            status = "🟢 활성" if cp['active'] else "🔴 비활성"
            uses_text = f"{cp['used_count']}/{cp['max_uses']}" if cp['max_uses'] > 0 else f"{cp['used_count']}/무제한"
            embed.add_field(
                name=f"{cp['code']} (-{cp['discount_amount']:,}포인트)",
                value=f"{status} | 사용: {uses_text}",
                inline=False
            )
        await interaction.response.send_message(embed=embed, ephemeral=True)
    
    @app_commands.command(name="쿠폰삭제", description="[관리자] 쿠폰을 삭제합니다")
    @app_commands.describe(code="삭제할 쿠폰 코드")
    async def coupon_delete_cmd(self, interaction: discord.Interaction, code: str):
        if not self.is_admin_check(interaction):
            await interaction.response.send_message("❌ 관리자만 사용할 수 있습니다.", ephemeral=True)
            return
        
        conn = db.get_conn()
        c = conn.cursor()
        c.execute('SELECT * FROM coupons WHERE code=?', (code.strip().upper(),))
        row = c.fetchone()
        conn.close()
        
        if not row:
            await interaction.response.send_message(
                f"❌ 존재하지 않는 쿠폰 코드입니다: **{code.strip().upper()}**",
                ephemeral=True
            )
            return
        
        db.delete_coupon(row['id'])
        await interaction.response.send_message(
            f"✅ 쿠폰이 삭제되었습니다: **{code.strip().upper()}**",
            ephemeral=True
        )
    
    @app_commands.command(name="공지", description="[관리자] 공지 채널에 임베드 공지를 보냅니다")
    @app_commands.describe(title="공지 제목", content="공지 내용")
    async def send_notice_cmd(self, interaction: discord.Interaction, title: str, content: str):
        if not self.is_admin_check(interaction):
            await interaction.response.send_message("❌ 관리자만 사용할 수 있습니다.", ephemeral=True)
            return
        
        await interaction.response.defer(ephemeral=True)
        success, msg = await self.bot.send_notice(title, content)
        await interaction.followup.send(msg, ephemeral=True)
    
    @app_commands.command(name="관리자추가", description="[관리자] 관리자를 추가합니다")
    @app_commands.describe(user="관리자로 추가할 유저")
    async def add_admin(self, interaction: discord.Interaction, user: discord.User):
        if not self.is_admin_check(interaction):
            await interaction.response.send_message("❌ 관리자만 사용할 수 있습니다.", ephemeral=True)
            return
        
        db.add_admin(str(user.id), user.name)
        await interaction.response.send_message(f"✅ {user.mention} 님이 관리자로 추가되었습니다!", ephemeral=True)
    
    @app_commands.command(name="설정확인", description="[관리자] 봇 설정을 확인합니다")
    async def check_settings(self, interaction: discord.Interaction):
        if not self.is_admin_check(interaction):
            await interaction.response.send_message("❌ 관리자만 사용할 수 있습니다.", ephemeral=True)
            return
        
        settings = db.get_all_settings()
        embed = discord.Embed(
            title="⚙️ 봇 설정",
            color=discord.Color.blue()
        )
        embed.add_field(name="봇 이름", value=settings.get('bot_name', '냥코 KEY 자판기'), inline=True)
        embed.add_field(name="입금 계좌", value=settings.get('bank_account', '미설정'), inline=True)
        embed.add_field(name="예금주", value=settings.get('bank_holder', '미설정'), inline=True)
        embed.add_field(name="Pushbullet", value="✅ 설정됨" if settings.get('pushbullet_token') else "❌ 미설정", inline=True)
        embed.add_field(name="대시보드 URL", value=settings.get('dashboard_url', '미설정'), inline=True)
        embed.add_field(name="문의 카테고리", value=settings.get('ticket_category_id', '미설정'), inline=True)
        await interaction.response.send_message(embed=embed, ephemeral=True)
    
    # ============ VENDING MACHINE TEXT EDIT COMMANDS ============
    
    @app_commands.command(name="자판기제목", description="[관리자] 자판기 제목을 변경합니다")
    @app_commands.describe(text="새 제목")
    async def set_vending_title(self, interaction: discord.Interaction, text: str):
        if not self.is_admin_check(interaction):
            await interaction.response.send_message("❌ 관리자만 사용할 수 있습니다.", ephemeral=True)
            return
        
        db.set_setting('vending_title', text)
        await interaction.response.send_message(f"✅ 자판기 제목이 변경되었습니다!\n**{text}**", ephemeral=True)
        await self.bot.refresh_vending_machines()
    
    @app_commands.command(name="자판기설명", description="[관리자] 자판기 설명을 변경합니다")
    @app_commands.describe(text="새 설명")
    async def set_vending_desc(self, interaction: discord.Interaction, text: str):
        if not self.is_admin_check(interaction):
            await interaction.response.send_message("❌ 관리자만 사용할 수 있습니다.", ephemeral=True)
            return
        
        db.set_setting('vending_desc', text)
        await interaction.response.send_message(f"✅ 자판기 설명이 변경되었습니다!", ephemeral=True)
        await self.bot.refresh_vending_machines()
    
    @app_commands.command(name="자판기푸터", description="[관리자] 자판기 하단 문구를 변경합니다")
    @app_commands.describe(text="새 하단 문구")
    async def set_vending_footer(self, interaction: discord.Interaction, text: str):
        if not self.is_admin_check(interaction):
            await interaction.response.send_message("❌ 관리자만 사용할 수 있습니다.", ephemeral=True)
            return
        
        db.set_setting('vending_footer', text)
        await interaction.response.send_message(f"✅ 자판기 하단 문구가 변경되었습니다!\n**{text}**", ephemeral=True)
        await self.bot.refresh_vending_machines()
    
    @app_commands.command(name="자판기색상", description="[관리자] 자판기 임베드 색상을 변경합니다 (예: #ff0000)")
    @app_commands.describe(color="색상 코드 (예: #3498db)")
    async def set_vending_color(self, interaction: discord.Interaction, color: str):
        if not self.is_admin_check(interaction):
            await interaction.response.send_message("❌ 관리자만 사용할 수 있습니다.", ephemeral=True)
            return
        
        try:
            color_hex = color.replace('#', '0x')
            int(color_hex, 16)
        except:
            await interaction.response.send_message("❌ 올바른 색상 코드를 입력해주세요. (예: #3498db)", ephemeral=True)
            return
        
        db.set_setting('vending_color', color)
        await interaction.response.send_message(f"✅ 자판기 색상이 변경되었습니다! ({color})", ephemeral=True)
        await self.bot.refresh_vending_machines()
    
    @app_commands.command(name="자판기상품제목", description="[관리자] 자판기 상품 섹션 제목을 변경합니다")
    @app_commands.describe(text="새 상품 섹션 제목")
    async def set_vending_product_title(self, interaction: discord.Interaction, text: str):
        if not self.is_admin_check(interaction):
            await interaction.response.send_message("❌ 관리자만 사용할 수 있습니다.", ephemeral=True)
            return
        
        db.set_setting('vending_product_title', text)
        await interaction.response.send_message(f"✅ 상품 섹션 제목이 변경되었습니다!\n**{text}**", ephemeral=True)
        await self.bot.refresh_vending_machines()
    
    # ============ PURCHASE DM MESSAGE EDIT COMMANDS ============
    
    @app_commands.command(name="구매메시지제목", description="[관리자] 구매 완료 DM 제목을 변경합니다")
    @app_commands.describe(text="새 DM 제목")
    async def set_purchase_dm_title(self, interaction: discord.Interaction, text: str):
        if not self.is_admin_check(interaction):
            await interaction.response.send_message("❌ 관리자만 사용할 수 있습니다.", ephemeral=True)
            return
        
        db.set_setting('purchase_dm_title', text)
        await interaction.response.send_message(f"✅ 구매 완료 DM 제목이 변경되었습니다!\n**{text}**", ephemeral=True)
    
    @app_commands.command(name="구매메시지설명", description="[관리자] 구매 완료 DM 설명을 변경합니다 ({product} = 상품명)")
    @app_commands.describe(text="새 DM 설명")
    async def set_purchase_dm_desc(self, interaction: discord.Interaction, text: str):
        if not self.is_admin_check(interaction):
            await interaction.response.send_message("❌ 관리자만 사용할 수 있습니다.", ephemeral=True)
            return
        
        db.set_setting('purchase_dm_desc', text)
        await interaction.response.send_message(f"✅ 구매 완료 DM 설명이 변경되었습니다!", ephemeral=True)
    
    @app_commands.command(name="구매메시지푸터", description="[관리자] 구매 완료 DM 하단 문구를 변경합니다")
    @app_commands.describe(text="새 DM 하단 문구")
    async def set_purchase_dm_footer(self, interaction: discord.Interaction, text: str):
        if not self.is_admin_check(interaction):
            await interaction.response.send_message("❌ 관리자만 사용할 수 있습니다.", ephemeral=True)
            return
        
        db.set_setting('purchase_dm_footer', text)
        await interaction.response.send_message(f"✅ 구매 완료 DM 하단 문구가 변경되었습니다!\n**{text}**", ephemeral=True)
    
    # ============ TICKET SETTINGS ============
    
    @app_commands.command(name="문의카테고리", description="[관리자] 문의 티켓이 생성될 카테고리를 설정합니다")
    @app_commands.describe(category="티켓이 생성될 카테고리")
    async def set_ticket_category(self, interaction: discord.Interaction, category: discord.CategoryChannel):
        if not self.is_admin_check(interaction):
            await interaction.response.send_message("❌ 관리자만 사용할 수 있습니다.", ephemeral=True)
            return
        
        db.set_setting('ticket_category_id', str(category.id))
        await interaction.response.send_message(
            f"✅ 문의 티켓 카테고리가 설정되었습니다!\n📌 {category.name}",
            ephemeral=True
        )
    
    @app_commands.command(name="문의목록", description="[관리자] 열려있는 문의 티켓 목록을 확인합니다")
    async def ticket_list(self, interaction: discord.Interaction):
        if not self.is_admin_check(interaction):
            await interaction.response.send_message("❌ 관리자만 사용할 수 있습니다.", ephemeral=True)
            return
        
        tickets = db.get_open_tickets()
        if not tickets:
            await interaction.response.send_message("✅ 열려있는 문의 티켓이 없습니다.", ephemeral=True)
            return
        
        embed = discord.Embed(
            title=f"🎫 열려있는 문의 티켓 ({len(tickets)}건)",
            color=discord.Color.blue()
        )
        for t in tickets[:10]:
            embed.add_field(
                name=f"#{t['id']} - {t['username']}",
                value=f"상태: 열림 | 생성: {t['created_at']}",
                inline=False
            )
        await interaction.response.send_message(embed=embed, ephemeral=True)
