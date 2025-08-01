# This file is part of Indico.
# Copyright (C) 2002 - 2025 CERN
#
# Indico is free software; you can redistribute it and/or
# modify it under the terms of the MIT License; see the
# LICENSE file for more details.

import itertools
import posixpath
import time
from decimal import Decimal
from email.mime.image import MIMEImage
from uuid import uuid4

from flask import has_request_context, request, session
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.event import listens_for
from sqlalchemy.ext.hybrid import hybrid_method, hybrid_property
from sqlalchemy.orm import column_property, mapper

from indico.core import signals
from indico.core.config import config
from indico.core.db import db
from indico.core.db.sqlalchemy import PyIntEnum, UTCDateTime
from indico.core.db.sqlalchemy.util.queries import increment_and_get
from indico.core.errors import IndicoError
from indico.core.storage import StoredFileMixin
from indico.modules.events.payment.models.transactions import TransactionStatus
from indico.modules.events.registration.models.items import PersonalDataType
from indico.modules.events.registration.wallets.apple import AppleWalletManager
from indico.modules.events.registration.wallets.google import GoogleWalletManager
from indico.modules.users.models.users import format_display_full_name
from indico.util.date_time import format_currency, now_utc
from indico.util.decorators import classproperty
from indico.util.enum import RichIntEnum
from indico.util.fs import secure_filename
from indico.util.i18n import L_, _
from indico.util.locators import locator_property
from indico.util.signals import values_from_signal
from indico.util.string import format_full_name, format_repr, strict_str
from indico.web.flask.util import url_for


class RegistrationState(RichIntEnum):
    __titles__ = [None, L_('Completed'), L_('Pending'), L_('Rejected'), L_('Withdrawn'), L_('Awaiting payment')]
    complete = 1
    pending = 2
    rejected = 3
    withdrawn = 4
    unpaid = 5


def _get_next_friendly_id(context):
    """Get the next friendly id for a registration."""
    from indico.modules.events import Event
    event_id = context.current_parameters['event_id']
    assert event_id is not None
    return increment_and_get(Event._last_friendly_registration_id, Event.id == event_id)


registrations_tags_table = db.Table(
    'registration_tags',
    db.metadata,
    db.Column(
        'registration_id',
        db.Integer,
        db.ForeignKey('event_registration.registrations.id', ondelete='CASCADE'),
        primary_key=True,
        nullable=False,
        index=True,
    ),
    db.Column(
        'registration_tag_id',
        db.Integer,
        db.ForeignKey('event_registration.tags.id', ondelete='CASCADE'),
        primary_key=True,
        nullable=False,
        index=True,
    ),
    schema='event_registration'
)


class PublishRegistrationsMode(RichIntEnum):
    __titles__ = [L_('Hide all participants'), L_('Show only consenting participants'), L_('Show all participants')]
    hide_all = 0
    show_with_consent = 1
    show_all = 2


class RegistrationVisibility(RichIntEnum):
    __titles__ = [L_('Do not display to anyone'), L_('Display to other participants'), L_('Display to everyone')]
    nobody = 0
    participants = 1
    all = 2


class Registration(db.Model):
    """Somebody's registration for an event through a registration form."""

    __tablename__ = 'registrations'
    __table_args__ = (db.CheckConstraint('email = lower(email)', 'lowercase_email'),
                      db.Index(None, 'friendly_id', 'event_id', unique=True,
                               postgresql_where=db.text('NOT is_deleted')),
                      db.Index(None, 'registration_form_id', 'user_id', unique=True,
                               postgresql_where=db.text('NOT is_deleted AND (state NOT IN (3, 4))')),
                      db.Index(None, 'registration_form_id', 'email', unique=True,
                               postgresql_where=db.text('NOT is_deleted AND (state NOT IN (3, 4))')),
                      db.ForeignKeyConstraint(['event_id', 'registration_form_id'],
                                              ['event_registration.forms.event_id', 'event_registration.forms.id']),
                      {'schema': 'event_registration'})

    #: The ID of the object
    id = db.Column(
        db.Integer,
        primary_key=True
    )
    #: The unguessable ID for the object
    uuid = db.Column(
        UUID,
        index=True,
        unique=True,
        nullable=False,
        default=lambda: str(uuid4())
    )
    #: The human-friendly ID for the object
    friendly_id = db.Column(
        db.Integer,
        nullable=False,
        default=_get_next_friendly_id
    )
    #: The ID of the event
    event_id = db.Column(
        db.Integer,
        db.ForeignKey('events.events.id'),
        index=True,
        nullable=False
    )
    #: The ID of the registration form
    registration_form_id = db.Column(
        db.Integer,
        db.ForeignKey('event_registration.forms.id'),
        index=True,
        nullable=False
    )
    #: The ID of the user who registered
    user_id = db.Column(
        db.Integer,
        db.ForeignKey('users.users.id'),
        index=True,
        nullable=True
    )
    #: The ID of the latest payment transaction associated with this registration
    transaction_id = db.Column(
        db.Integer,
        db.ForeignKey('events.payment_transactions.id'),
        index=True,
        unique=True,
        nullable=True
    )
    #: The state a registration is in
    state = db.Column(
        PyIntEnum(RegistrationState),
        nullable=False,
    )
    #: The base registration fee (that is not specific to form items)
    base_price = db.Column(
        db.Numeric(11, 2),  # max. 999999999.99
        nullable=False,
        default=0
    )
    #: The price modifier applied to the final calculated price
    price_adjustment = db.Column(
        db.Numeric(11, 2),  # max. 999999999.99
        nullable=False,
        default=0
    )
    #: Registration price currency
    currency = db.Column(
        db.String,
        nullable=False
    )
    #: The date/time when the registration was recorded
    submitted_dt = db.Column(
        UTCDateTime,
        nullable=False,
        default=now_utc,
    )
    #: The email of the registrant
    email = db.Column(
        db.String,
        nullable=False
    )
    #: The first name of the registrant
    first_name = db.Column(
        db.String,
        nullable=False
    )
    #: The last name of the registrant
    last_name = db.Column(
        db.String,
        nullable=False
    )
    #: If the registration has been deleted
    is_deleted = db.Column(
        db.Boolean,
        nullable=False,
        default=False
    )
    #: The unique token used in tickets
    ticket_uuid = db.Column(
        UUID,
        index=True,
        unique=True,
        nullable=False,
        default=lambda: str(uuid4())
    )
    #: Whether the person has checked in. Setting this also sets or clears
    #: `checked_in_dt`.
    checked_in = db.Column(
        db.Boolean,
        nullable=False,
        default=False
    )
    #: The date/time when the person has checked in
    checked_in_dt = db.Column(
        UTCDateTime,
        nullable=True
    )
    #: If given a reason for rejection
    rejection_reason = db.Column(
        db.String,
        nullable=False,
        default='',
    )
    #: Type of consent given to publish this registration
    consent_to_publish = db.Column(
        PyIntEnum(RegistrationVisibility),
        nullable=False,
        default=RegistrationVisibility.nobody
    )
    #: Management-set override for visibility
    participant_hidden = db.Column(
        db.Boolean,
        nullable=False,
        default=False
    )
    #: Whether the registration was created by a manager
    created_by_manager = db.Column(
        db.Boolean,
        nullable=False,
        default=False
    )
    #: The date/time until which the person can modify their registration
    modification_end_dt = db.Column(
        UTCDateTime,
        nullable=True
    )

    #: The Event containing this registration
    event = db.relationship(
        'Event',
        lazy=True,
        backref=db.backref(
            'registrations',
            lazy='dynamic'
        )
    )
    # The user linked to this registration
    user = db.relationship(
        'User',
        lazy=True,
        backref=db.backref(
            'registrations',
            lazy='dynamic'
            # XXX: a delete-orphan cascade here would delete registrations when NULLing the user
        )
    )
    #: The latest payment transaction associated with this registration
    transaction = db.relationship(
        'PaymentTransaction',
        lazy=True,
        foreign_keys=[transaction_id],
        post_update=True
    )
    #: The registration this data is associated with
    data = db.relationship(
        'RegistrationData',
        lazy=True,
        cascade='all, delete-orphan',
        backref=db.backref(
            'registration',
            lazy=True
        )
    )
    #: The registration tags assigned to this registration
    tags = db.relationship(
        'RegistrationTag',
        secondary=registrations_tags_table,
        passive_deletes=True,
        collection_class=set,
        order_by='RegistrationTag.title',
        backref=db.backref(
            'registrations',
            lazy=True
        )
    )
    #: The serial number assigned to the Apple Wallet pass
    apple_wallet_serial = db.Column(
        db.String,
        nullable=False,
        default='',
    )
    # relationship backrefs:
    # - invitation (RegistrationInvitation.registration)
    # - legacy_mapping (LegacyRegistrationMapping.registration)
    # - receipt_files (ReceiptFile.registration)
    # - registration_form (RegistrationForm.registrations)
    # - transactions (PaymentTransaction.registration)

    @classmethod
    def get_all_for_event(cls, event):
        """Retrieve all registrations in all registration forms of an event."""
        from indico.modules.events.registration.models.forms import RegistrationForm
        return (Registration.query
                .filter(Registration.is_active,
                        ~RegistrationForm.is_deleted,
                        RegistrationForm.event_id == event.id)
                .join(Registration.registration_form)
                .all())

    @classmethod
    def merge_users(cls, target, source):
        target_regforms_used = {r.registration_form for r in target.registrations if not r.is_deleted}
        for r in source.registrations.all():
            if r.registration_form not in target_regforms_used:
                r.user = target

    @hybrid_method
    def is_publishable(self, is_participant):
        if self.visibility == RegistrationVisibility.nobody or not self.is_state_publishable:
            return False
        if (self.registration_form.publish_registrations_duration is not None
                and self.event.end_dt + self.registration_form.publish_registrations_duration <= now_utc()):
            return False
        if self.visibility == RegistrationVisibility.participants:
            return is_participant
        if self.visibility == RegistrationVisibility.all:
            return True
        return False

    @is_publishable.expression
    def is_publishable(cls, is_participant):
        from indico.modules.events import Event
        from indico.modules.events.registration.models.forms import RegistrationForm

        def _has_regform_publish_mode(mode):
            if is_participant:
                return cls.registration_form.has(publish_registrations_participants=mode)
            else:
                return cls.registration_form.has(publish_registrations_public=mode)
        consent_criterion = (
            cls.consent_to_publish.in_([RegistrationVisibility.all, RegistrationVisibility.participants])
            if is_participant else
            cls.consent_to_publish == RegistrationVisibility.all
        )
        return db.and_(
            ~cls.participant_hidden,
            cls.is_state_publishable,
            cls.registration_form.has(db.or_(
                RegistrationForm.publish_registrations_duration.is_(None),
                cls.event.has((Event.end_dt + RegistrationForm.publish_registrations_duration) > now_utc())
            )),
            ~_has_regform_publish_mode(PublishRegistrationsMode.hide_all),
            _has_regform_publish_mode(PublishRegistrationsMode.show_all) | consent_criterion
        )

    @hybrid_property
    def is_active(self):
        return not self.is_cancelled and not self.is_deleted

    @is_active.expression
    def is_active(cls):
        return ~cls.is_cancelled & ~cls.is_deleted

    @hybrid_property
    def is_cancelled(self):
        return self.state in (RegistrationState.rejected, RegistrationState.withdrawn)

    @is_cancelled.expression
    def is_cancelled(cls):
        return cls.state.in_((RegistrationState.rejected, RegistrationState.withdrawn))

    @hybrid_property
    def is_state_publishable(self):
        return self.is_active and self.state in (RegistrationState.complete, RegistrationState.unpaid)

    @is_state_publishable.expression
    def is_state_publishable(cls):
        return cls.is_active & cls.state.in_((RegistrationState.complete, RegistrationState.unpaid))

    @locator_property
    def locator(self):
        return dict(self.registration_form.locator, registration_id=self.id)

    @locator.registrant
    def locator(self):
        """A locator suitable for 'display' pages.

        It includes the UUID of the registration unless the current
        request doesn't contain the uuid and the registration is tied
        to the currently logged-in user.
        """
        loc = self.registration_form.locator
        if (not self.user or not has_request_context() or self.user != session.user or
                request.args.get('token') == self.uuid):
            loc['token'] = self.uuid
        return loc

    def is_field_shown(self, field):
        from indico.modules.events.registration.util import is_conditional_field_shown
        return is_conditional_field_shown(field, self.data_by_field, is_db_data=True)

    @locator.uuid
    def locator(self):
        """A locator that uses uuid instead of id."""
        return dict(self.registration_form.locator, token=self.uuid)

    @property
    def modification_deadline_passed(self):
        if self.modification_end_dt is None:
            return True
        return self.modification_end_dt < now_utc()

    @property
    def can_be_modified(self):
        return self.registration_form.is_modification_allowed(self)

    @property
    def can_be_withdrawn(self):
        from indico.modules.events.registration.models.forms import ModificationMode
        if not self.is_active:
            return False
        elif self.is_paid:
            return False
        elif self.event.end_dt < now_utc():
            return False
        elif self.registration_form.modification_mode == ModificationMode.not_allowed:
            return False
        elif self.registration_form.modification_end_dt and self.registration_form.modification_end_dt < now_utc():
            return False
        else:
            return True

    @property
    def data_by_field(self):
        return {x.field_data.field_id: x for x in self.data}

    @property
    def billable_data(self):
        return [data for data in self.data if data.price]

    @property
    def full_name(self):
        """Return the user's name in 'Firstname Lastname' notation."""
        return self.get_full_name(last_name_first=False)

    @property
    def display_full_name(self):
        """Return the full name using the user's preferred name format."""
        return format_display_full_name(session.user, self)

    @property
    def avatar_url(self):
        """Return the url of the user's avatar."""
        return url_for('event_registration.registration_avatar', self)

    @property
    def external_registration_details_url(self):
        return url_for('event_registration.registration_details', self, _external=True)

    @property
    def display_regform_url(self):
        return url_for('event_registration.display_regform', self.locator.registrant, _external=True)

    @property
    def is_ticket_blocked(self):
        """Check whether the ticket is blocked by a plugin."""
        return any(values_from_signal(signals.event.is_ticket_blocked.send(self), single_value=True))

    @property
    def google_wallet_ticket_id(self):
        return f'Ticket-{self.event_id}-{self.registration_form_id}-{self.id}'

    @property
    def is_paid(self):
        """Return whether the registration has been paid for."""
        paid_states = {TransactionStatus.successful, TransactionStatus.pending}
        return self.transaction is not None and self.transaction.status in paid_states

    @property
    def payment_dt(self):
        """The date/time when the registration has been paid for."""
        return self.transaction.timestamp if self.is_paid else None

    @property
    def price(self):
        """The total price of the registration.

        This includes the base price, the field-specific price, and
        the custom price adjustment for the registrant.

        :rtype: Decimal
        """
        # we convert the calculated price (float) to a string to avoid this:
        # >>> Decimal(100.1)
        # Decimal('100.099999999999994315658113919198513031005859375')
        # >>> Decimal('100.1')
        # Decimal('100.1')
        calc_price = Decimal(str(sum(data.price for data in self.data)))
        base_price = self.base_price or Decimal(0)
        price_adjustment = self.price_adjustment or Decimal(0)
        return (base_price + price_adjustment + calc_price).max(0)

    def get_summary_data(self, *, hide_empty=False):
        """Export registration data nested in sections and fields."""

        def _fill_from_regform():
            sections = self.sections_with_answered_fields if hide_empty else self.registration_form.sections
            for section in sections:
                if not section.is_visible:
                    continue
                summary[section] = {}
                for field in section.fields:
                    if not field.is_visible:
                        continue
                    if field in field_summary:
                        summary[section][field] = field_summary[field]

        def _fill_from_registration():
            for field, data in field_summary.items():
                section = field.parent
                summary.setdefault(section, {})
                if field not in summary[section]:
                    summary[section][field] = data

        summary = {}
        field_summary = {x.field_data.field: x for x in self.data}
        _fill_from_regform()
        _fill_from_registration()
        return summary

    @property
    def has_files(self):
        return any(item.storage_file_id is not None for item in self.data)

    @property
    def sections_with_answered_fields(self):
        return [x for x in self.registration_form.sections
                if any(child.id in self.data_by_field for child in x.children)]

    @property
    def visibility_before_override(self):
        if self.registration_form.publish_registrations_participants == PublishRegistrationsMode.hide_all:
            return RegistrationVisibility.nobody
        vis = self.consent_to_publish
        if self.registration_form.publish_registrations_participants == PublishRegistrationsMode.show_all:
            if self.registration_form.publish_registrations_public == PublishRegistrationsMode.hide_all:
                return RegistrationVisibility.participants
            if self.registration_form.publish_registrations_public == PublishRegistrationsMode.show_all:
                return RegistrationVisibility.all
            return max(vis, RegistrationVisibility.participants)
        elif self.registration_form.publish_registrations_public == PublishRegistrationsMode.hide_all:
            return min(vis, RegistrationVisibility.participants)
        return vis

    @property
    def visibility(self):
        if self.participant_hidden:
            return RegistrationVisibility.nobody
        return self.visibility_before_override

    @property
    def accompanying_persons(self):
        from indico.modules.events.registration.models.form_fields import RegistrationFormFieldData
        query = (RegistrationData.query
                 .with_parent(self)
                 .join(RegistrationFormFieldData)
                 .filter(RegistrationFormFieldData.field.has(input_type='accompanying_persons')))
        return list(itertools.chain.from_iterable(d.data for d in query.all() if not d.field_data.field.is_deleted))

    @property
    def published_receipts(self):
        return [receipt for receipt in self.receipt_files if receipt.is_published]

    @classproperty
    @classmethod
    def order_by_name(cls):
        return db.func.lower(cls.last_name), db.func.lower(cls.first_name), cls.friendly_id

    def __repr__(self):
        return format_repr(self, 'id', 'registration_form_id', 'email', 'state',
                           user_id=None, is_deleted=False, _text=self.full_name)

    def get_full_name(self, *, last_name_first=True, last_name_upper=False, abbrev_first_name=False):
        """Return the user's name in the specified notation.

        If no format options are specified, the name is returned in
        the 'Lastname, Firstname' notation.

        :param last_name_first: if "lastname, firstname" instead of
                                "firstname lastname" should be used
        :param last_name_upper: if the last name should be all-uppercase
        :param abbrev_first_name: if the first name should be abbreviated to
                                  use only the first character
        """
        return format_full_name(self.first_name, self.last_name,
                                last_name_first=last_name_first, last_name_upper=last_name_upper,
                                abbrev_first_name=abbrev_first_name)

    def get_personal_data(self):
        # the personal data picture is not included in this method since it's rather
        # useless here. use `get_personal_data_picture` to get it when needed
        personal_data = {}
        for data in self.data:
            field = data.field_data.field
            if field.personal_data_type is not None and data.data:
                personal_data[field.personal_data_type.name] = data.friendly_data
        # might happen with imported legacy registrations (missing personal data)
        personal_data.setdefault('first_name', self.first_name)
        personal_data.setdefault('last_name', self.last_name)
        personal_data.setdefault('email', self.email)
        return personal_data

    def get_personal_data_picture(self):
        """Return the picture data in personal data."""
        rdata = next((d for d in self.data if d.field_data.field.personal_data_type == PersonalDataType.picture), None)
        if rdata and rdata.storage_file_id is not None:
            return rdata

    def get_picture_attachments(self, *, personal_data_only=False):
        """Return a list of registration pictures as `MimeImage` attachments.

        :param personal_data_only: If True, return only the main picture from personal data
        """
        from indico.modules.events.registration.util import process_registration_picture
        picture_attachements = []
        for data in self.data:
            if not data.field_data.field.is_active or data.field_data.field.input_type != 'picture':
                continue
            if data.field_data.field.parent.is_manager_only:
                continue
            if data.storage_file_id is None:
                continue
            if personal_data_only and not data.field_data.field.personal_data_type:
                continue
            with data.open() as f:
                if not (thumbnail_bytes := process_registration_picture(f, thumbnail=True)):
                    continue
                attachment = MIMEImage(thumbnail_bytes.read(), 'jpeg')
            attachment.add_header('Content-ID', f'<{data.attachment_cid}>')
            picture_attachements.append(attachment)
        return picture_attachements

    def _render_price(self, price):
        return format_currency(price, self.currency)

    def render_price(self):
        return self._render_price(self.price)

    def render_base_price(self):
        return self._render_price(self.base_price)

    def render_price_adjustment(self):
        return self._render_price(self.price_adjustment)

    def sync_state(self, _skip_moderation=True):
        """Sync the state of the registration."""
        initial_state = self.state
        regform = self.registration_form
        invitation = self.invitation
        moderation_required = (regform.moderation_enabled and not _skip_moderation and
                               (not invitation or not invitation.skip_moderation))
        with db.session.no_autoflush:
            payment_required = regform.event.has_feature('payment') and self.price and not self.is_paid
        if self.state is None:
            if moderation_required:
                self.state = RegistrationState.pending
            elif payment_required:
                self.state = RegistrationState.unpaid
            else:
                self.state = RegistrationState.complete
        elif self.state == RegistrationState.unpaid:
            if not self.price:
                self.state = RegistrationState.complete
        elif self.state == RegistrationState.complete:
            if payment_required:
                self.state = RegistrationState.unpaid
        if self.state != initial_state:
            signals.event.registration_state_updated.send(self, previous_state=initial_state)

    def update_state(self, approved=None, paid=None, rejected=None, withdrawn=None, _skip_moderation=False):
        """Update the state of the registration for a given action.

        The accepted kwargs are the possible actions. ``True`` means that the
        action occured and ``False`` that it was reverted.
        """
        if sum(action is not None for action in (approved, paid, rejected, withdrawn)) > 1:
            raise Exception('More than one action specified')
        initial_state = self.state
        regform = self.registration_form
        invitation = self.invitation
        moderation_required = (regform.moderation_enabled and not _skip_moderation and
                               (not invitation or not invitation.skip_moderation))
        with db.session.no_autoflush:
            payment_required = regform.event.has_feature('payment') and bool(self.price)
        if self.state == RegistrationState.pending:
            if approved and payment_required:
                self.state = RegistrationState.unpaid
            elif approved:
                self.state = RegistrationState.complete
            elif rejected:
                self.state = RegistrationState.rejected
            elif withdrawn:
                self.state = RegistrationState.withdrawn
        elif self.state == RegistrationState.unpaid:
            if paid:
                self.state = RegistrationState.complete
            elif approved is False:
                self.state = RegistrationState.pending
            elif withdrawn:
                self.state = RegistrationState.withdrawn
        elif self.state == RegistrationState.complete:
            if approved is False and payment_required is False and moderation_required:
                self.state = RegistrationState.pending
            elif paid is False and payment_required:
                self.state = RegistrationState.unpaid
            elif withdrawn:
                self.state = RegistrationState.withdrawn
        elif self.state == RegistrationState.rejected:
            if rejected is False and moderation_required:
                self.state = RegistrationState.pending
            elif rejected is False and payment_required:
                self.state = RegistrationState.unpaid
            elif rejected is False:
                self.state = RegistrationState.complete
        elif self.state == RegistrationState.withdrawn:
            if withdrawn is False and moderation_required:
                self.state = RegistrationState.pending
            elif withdrawn is False and payment_required:
                self.state = RegistrationState.unpaid
            elif withdrawn is False:
                self.state = RegistrationState.complete
        if self.state != initial_state:
            signals.event.registration_state_updated.send(self, previous_state=initial_state)

    def reset_state(self):
        """Reset the state of the registration back to pending."""
        if self.has_conflict():
            raise IndicoError(_('Cannot reset this registration since there is another valid registration for the '
                                'same user or email.'))
        if self.state in (RegistrationState.complete, RegistrationState.unpaid):
            self.update_state(approved=False)
        elif self.state == RegistrationState.rejected:
            self.rejection_reason = ''
            self.update_state(rejected=False)
        elif self.state == RegistrationState.withdrawn:
            self.update_state(withdrawn=False)
        elif self.state != RegistrationState.pending:
            raise ValueError(f'Cannot reset registration state from {self.state.name}')
        self.checked_in = False

    def has_conflict(self):
        """Check if there are other valid registrations for the same user.

        This is intended for cases where this registration is currenly invalid
        (rejected or withdrawn) to determine whether it would be acceptable to
        restore it.
        """
        conflict_criteria = [Registration.email == self.email]
        if self.user_id is not None:
            conflict_criteria.append(Registration.user_id == self.user_id)
        return (Registration.query
                .with_parent(self.registration_form)
                .filter(Registration.id != self.id,
                        ~Registration.is_deleted,
                        db.or_(*conflict_criteria),
                        Registration.state.notin_([RegistrationState.rejected, RegistrationState.withdrawn]))
                .has_rows())

    def log(self, *args, **kwargs):
        """Log with prefilled metadata for the registration."""
        return self.event.log(*args, meta={'registration_id': self.id}, **kwargs)

    def is_pending_transaction_expired(self):
        """Check if the registration has a pending transaction that expired."""
        if not self.transaction or self.transaction.status != TransactionStatus.pending:
            return False
        return self.transaction.is_pending_expired()

    def generate_ticket_google_wallet_url(self):
        """Return link to Google Wallet ticket display."""
        if not self.registration_form.ticket_google_wallet:
            return None
        gwm = GoogleWalletManager(self.event)
        if gwm.is_configured:
            return gwm.get_ticket_link(self)

    def generate_ticket_apple_wallet(self):
        """Download ticket in Passbook / Apple Wallet format."""
        if not self.registration_form.ticket_apple_wallet:
            return None
        awm = AppleWalletManager(self.event)
        if awm.is_configured:
            return awm.get_ticket(self)


class RegistrationData(StoredFileMixin, db.Model):
    """Data entry within a registration for a field in a registration form."""

    __tablename__ = 'registration_data'
    __table_args__ = {'schema': 'event_registration'}

    # StoredFileMixin settings
    add_file_date_column = False
    file_required = False

    #: The ID of the registration
    registration_id = db.Column(
        db.Integer,
        db.ForeignKey('event_registration.registrations.id'),
        primary_key=True,
        autoincrement=False
    )
    #: The ID of the field data
    field_data_id = db.Column(
        db.Integer,
        db.ForeignKey('event_registration.form_field_data.id'),
        primary_key=True,
        autoincrement=False
    )
    #: The submitted data for the field
    data = db.Column(
        JSONB,
        default=lambda: None,
        nullable=False
    )

    #: The associated field data object
    field_data = db.relationship(
        'RegistrationFormFieldData',
        lazy=True,
        backref=db.backref(
            'registration_data',
            lazy=True,
            cascade='all, delete-orphan'
        )
    )

    # relationship backrefs:
    # - registration (Registration.data)

    @locator_property
    def locator(self):
        # a normal locator doesn't make much sense
        raise NotImplementedError

    @locator.file
    def locator(self):
        """A locator that points to the associated file."""
        if not self.filename:
            raise Exception('The file locator is only available if there is a file.')
        return dict(self.registration.locator, field_data_id=self.field_data_id, filename=self.filename)

    @locator.registrant_file
    def locator(self):
        """A locator that points to the associated file for a registrant."""
        if not self.filename:
            raise Exception('The file locator is only available if there is a file.')
        return dict(self.registration.locator.registrant, registration_id=self.registration.id,
                    field_data_id=self.field_data_id, filename=self.filename)

    @property
    def attachment_cid(self):
        """A Content-ID suitable for email attachments.

        This is meant for registration data that's linked to a picture file so it
        can be attached to a notification email and referenced inside that email.
        """
        return f'picture-{self.registration_id}-{self.field_data_id}'

    @property
    def friendly_data(self):
        return self.get_friendly_data()

    @property
    def search_data(self):
        return self.get_friendly_data(for_search=True)

    def get_friendly_data(self, **kwargs):
        return self.field_data.field.get_friendly_data(self, **kwargs)

    @property
    def price(self):
        return self.field_data.field.calculate_price(self)

    @property
    def user_data(self):
        from indico.modules.events.registration.fields.simple import KEEP_EXISTING_FILE_UUID
        if self.field_data.field.field_impl.is_file_field:
            return KEEP_EXISTING_FILE_UUID if self.storage_file_id is not None else None
        return self.data

    def _set_file(self, file):
        # in case we are deleting/replacing a file
        self.storage_backend = None
        self.storage_file_id = None
        self.filename = None
        self.content_type = None
        self.size = None
        if file:
            self.filename = file.filename
            self.content_type = file.content_type
            with file.open() as f:
                self.save(f)

    file = property(fset=_set_file)
    del _set_file

    def __repr__(self):
        return f'<RegistrationData({self.registration_id}, {self.field_data_id}): {self.data}>'

    def _build_storage_path(self):
        self.registration.registration_form.assign_id()
        self.registration.assign_id()
        path_segments = ['event', strict_str(self.registration.event_id), 'registrations',
                         strict_str(self.registration.registration_form.id), strict_str(self.registration.id)]
        assert None not in path_segments
        # add timestamp in case someone uploads the same file again
        filename = '{}-{}-{}'.format(self.field_data.field_id, int(time.time()), secure_filename(self.filename, 'file'))
        path = posixpath.join(*path_segments, filename)
        return config.ATTACHMENT_STORAGE, path

    def _render_price(self, price):
        return format_currency(price, self.registration.currency)

    def render_price(self):
        return self._render_price(self.price)


@listens_for(mapper, 'after_configured', once=True)
def _mapper_configured():
    from indico.modules.events.registration.models.form_fields import RegistrationFormFieldData
    from indico.modules.events.registration.models.items import RegistrationFormItem
    from indico.modules.receipts.models.files import ReceiptFile

    @listens_for(Registration.registration_form, 'set')
    def _set_event_id(target, value, *unused):
        target.event_id = value.event_id

    @listens_for(Registration.checked_in, 'set')
    def _set_checked_in_dt(target, value, *unused):
        if not value:
            target.checked_in_dt = None
        elif target.checked_in != value:
            target.checked_in_dt = now_utc()

    @listens_for(Registration.transaction, 'set')
    def _set_transaction_id(target, value, *unused):
        value.registration = target

    query = (select([db.func.coalesce(db.func.sum(db.func.jsonb_array_length(RegistrationData.data)), 0) + 1])
             .where(db.and_(RegistrationData.registration_id == Registration.id,
                            RegistrationData.field_data_id == RegistrationFormFieldData.id,
                            RegistrationFormFieldData.field_id == RegistrationFormItem.id,
                            RegistrationFormItem.input_type == 'accompanying_persons',
                            ~RegistrationFormItem.is_deleted,
                            db.cast(RegistrationFormItem.data['persons_count_against_limit'].astext, db.Boolean)))
             .correlate_except(RegistrationData)
             .scalar_subquery())
    Registration.occupied_slots = column_property(query, deferred=True)

    query = (select([db.func.coalesce(db.func.sum(db.func.jsonb_array_length(RegistrationData.data)), 0)])
             .where(db.and_(RegistrationData.registration_id == Registration.id,
                            RegistrationData.field_data_id == RegistrationFormFieldData.id,
                            RegistrationFormFieldData.field_id == RegistrationFormItem.id,
                            RegistrationFormItem.input_type == 'accompanying_persons',
                            ~RegistrationFormItem.is_deleted))
             .correlate_except(RegistrationData)
             .scalar_subquery())
    Registration.num_accompanying_persons = column_property(query, deferred=True)

    query = (select([db.func.coalesce(db.func.count(ReceiptFile.file_id), 0)])
             .where(db.and_(ReceiptFile.registration_id == Registration.id,
                            ~ReceiptFile.is_deleted))
             .correlate_except(ReceiptFile)
             .scalar_subquery())
    Registration.num_receipt_files = column_property(query, deferred=True)
