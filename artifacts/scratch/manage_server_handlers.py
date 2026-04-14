@router.callback_query(ServerActionCallback.filter(F.action == "manage"))
async def server_manage_menu(
    callback: CallbackQuery,
    callback_data: ServerActionCallback,
    session: AsyncSession,
) -> None:
    await callback.answer()
    server = await session.scalar(
        select(XUIServerRecord)
        .options(selectinload(XUIServerRecord.inbounds))
        .where(XUIServerRecord.id == callback_data.server_id)
    )
    if server is None:
        await callback.message.answer(AdminMessages.SERVER_NOT_FOUND)
        return

    active_inbounds = sum(1 for inbound in server.inbounds if inbound.is_active)
    
    # Active clients using this server
    active_client_count = int(
        await session.scalar(
            select(func.count())
            .select_from(XUIClientRecord)
            .join(XUIInboundRecord, XUIClientRecord.inbound_id == XUIInboundRecord.id)
            .where(
                XUIClientRecord.is_active.is_(True),
                XUIInboundRecord.server_id == server.id,
            )
        ) or 0
    )

    limit_text = str(server.max_clients) if server.max_clients else "نامحدود"
    status_text = "حذف شده" if server.health_status == "deleted" else (Common.ACTIVE if server.is_active else Common.INACTIVE)

    text = (
        f"🖥 **مدیریت سرور: {server.name}**\n\n"
        f"وضعیت: {status_text}\n"
        f"آدرس: {server.base_url}\n"
        f"دامنه کانفیگ: {server.config_domain or 'تنظیم نشده (پیش‌فرض آدرس پنل)'}\n"
        f"ساب دامین: {server.sub_domain or 'تنظیم نشده (پیش‌فرض آدرس پنل)'}\n\n"
        f"اینباندهای فعال: {active_inbounds}\n"
        f"کاربران فعال روی سرور: {active_client_count} / {limit_text}\n"
    )

    builder = InlineKeyboardBuilder()
    
    if server.health_status != "deleted":
        builder.button(
            text=f"🔄 سینک کردن پنل",
            callback_data=ServerActionCallback(action="sync", server_id=server.id, page=callback_data.page).pack(),
        )
        builder.button(
            text="🛑 تغییر وضعیت (ON/OFF)",
            callback_data=ServerActionCallback(action="toggle", server_id=server.id, page=callback_data.page).pack(),
        )
        builder.button(
            text="🌐 تنظیم دامنه‌ها",
            callback_data=ServerActionCallback(action="edit_domain", server_id=server.id, page=callback_data.page).pack(),
        )
        builder.button(
            text="👥 تنظیم محدودیت کاربر",
            callback_data=ServerActionCallback(action="edit_limit", server_id=server.id, page=callback_data.page).pack(),
        )
        builder.button(
            text=f"{AdminButtons.DELETE} حذف سرور",
            callback_data=ServerActionCallback(action="delete", server_id=server.id, page=callback_data.page).pack(),
        )

    builder.button(
        text="🔙 بازگشت به لیست",
        callback_data=ServerListPageCallback(page=callback_data.page).pack(),
    )
    
    builder.adjust(1)
    await _edit_or_send(callback, text=text, reply_markup=builder.as_markup())


@router.callback_query(ServerActionCallback.filter(F.action == "edit_domain"))
async def edit_domain_start(
    callback: CallbackQuery,
    callback_data: ServerActionCallback,
    state: FSMContext,
) -> None:
    await callback.answer()
    await state.update_data(server_id=str(callback_data.server_id), page=callback_data.page)
    await state.set_state(ServerManageStates.waiting_for_config_domain)
    
    builder = InlineKeyboardBuilder()
    builder.button(text="پرش (حذف مقدار)", callback_data="server:domain:skip_config")
    builder.adjust(1)
    
    await callback.message.answer(
        "ابتدا دامنه یا آدرس IP که برای ساخت کانفیگ‌ها (VLESS/VMess) استفاده می‌شود را وارد کنید.\n"
        "(مثلاً proxy.example.com یا آدرس IP تمیز)\n"
        "اگر مقداری ارسال نکنید، از همون آدرس پنل X-UI استفاده می‌شود.",
        reply_markup=builder.as_markup()
    )


@router.message(ServerManageStates.waiting_for_config_domain)
async def edit_domain_config(message: Message, state: FSMContext) -> None:
    if not message.text:
        return
    await state.update_data(config_domain=message.text.strip())
    await _prompt_sub_domain(message, state)


@router.callback_query(F.data == "server:domain:skip_config")
async def skip_config_domain(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.update_data(config_domain=None)
    await _prompt_sub_domain(callback.message, state)


async def _prompt_sub_domain(message: Message, state: FSMContext) -> None:
    await state.set_state(ServerManageStates.waiting_for_sub_domain)
    builder = InlineKeyboardBuilder()
    builder.button(text="پرش (حذف مقدار)", callback_data="server:domain:skip_sub")
    builder.adjust(1)
    await message.answer(
        "حالا ساب‌دامینی که برای لینک‌های اشتراک (Subscription Link) استفاده می‌شود را وارد کنید.\n"
        "(مثلاً sub.example.com)\n"
        "اگر مقداری ارسال نکنید، از همون آدرس پنل X-UI استفاده می‌شود.",
        reply_markup=builder.as_markup()
    )


@router.message(ServerManageStates.waiting_for_sub_domain)
async def edit_domain_sub(message: Message, state: FSMContext, session: AsyncSession) -> None:
    if not message.text:
        return
    await _save_domains(message, state, session, message.text.strip())


@router.callback_query(F.data == "server:domain:skip_sub")
async def skip_sub_domain(callback: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    await callback.answer()
    await _save_domains(callback.message, state, session, None)


async def _save_domains(message: Message, state: FSMContext, session: AsyncSession, sub_domain: str | None) -> None:
    data = await state.get_data()
    await state.clear()
    
    server_id = UUID(data["server_id"])
    config_domain = data.get("config_domain")
    
    server = await session.get(XUIServerRecord, server_id)
    if server:
        server.config_domain = config_domain
        server.sub_domain = sub_domain
        await session.flush()
        
    await message.answer(
        f"✅ دامنه‌های سرور ثبت شد.\n\nدامنه کانفیگ: {config_domain or 'تهی'}\nساب‌دامین: {sub_domain or 'تهی'}"
    )


@router.callback_query(ServerActionCallback.filter(F.action == "edit_limit"))
async def edit_limit_start(
    callback: CallbackQuery,
    callback_data: ServerActionCallback,
    state: FSMContext,
) -> None:
    await callback.answer()
    await state.update_data(server_id=str(callback_data.server_id), page=callback_data.page)
    await state.set_state(ServerManageStates.waiting_for_max_clients)
    
    builder = InlineKeyboardBuilder()
    builder.button(text="نامحدود (بدون لیمیت)", callback_data="server:limit:unlimited")
    builder.adjust(1)
    
    await callback.message.answer(
        "حداکثر تعداد کلاینت (کاربر فعال) مجاز روی این سرور را به عدد ارسال کنید.\n"
        "مثلاً: 100",
        reply_markup=builder.as_markup()
    )


@router.callback_query(F.data == "server:limit:unlimited")
async def limit_unlimited(callback: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    await callback.answer()
    await _save_limit(callback.message, state, session, None)


@router.message(ServerManageStates.waiting_for_max_clients)
async def edit_limit_value(message: Message, state: FSMContext, session: AsyncSession) -> None:
    if not message.text:
        return
    try:
        limit = int(message.text.strip())
        if limit < 0:
            raise ValueError
    except ValueError:
        await message.answer("لطفاً یک عدد معتبر و مثبت ارسال کنید.")
        return
    await _save_limit(message, state, session, limit)


async def _save_limit(message: Message, state: FSMContext, session: AsyncSession, limit: int | None) -> None:
    data = await state.get_data()
    await state.clear()
    
    server_id = UUID(data["server_id"])
    server = await session.get(XUIServerRecord, server_id)
    if server:
        server.max_clients = limit
        await session.flush()
        
    await message.answer(
        f"✅ محدودیت کاربر سرور روی {limit if limit is not None else 'نامحدود'} تنظیم شد."
    )
